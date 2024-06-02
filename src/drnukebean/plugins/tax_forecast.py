"""
A beancount plugin to make predictions on tax liabilities in kanton zurich switzerland

it uses a very messy config string like

'{"taxable_accounts": ["Income:Jobs:Taxable:Salary", "Income:Jobs:Taxable:Bonus", "Income:Invest:IB:.*:Div"], "deductable_accounts": [], "taxable_assets_accounts": [], "tax_expenses_main_account": "Expenses:Taxes", "liability_account": "Liabilities:Tax", "year": 2022, "api_year":2022 "assets": 200000, "taxable_income": 100000, "witholding": 500, "municipality": 261, "marial_srtatus": "single", "n_children": 0, "tax_day_of_month": 24, "precision": 2}'

that can be generated with json.dumps(<config_dict>)

use the plugin in your ledger file like: plugin "drnukebean.plugins.tax_forecast" <config_string>
"""

from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.data import Transaction
from beancount.core.number import Decimal

from beancount.query.query import run_query

import datetime
import pandas as pd
import json
import re
import http.client as httplib
import traceback
import pickle
import os
import hashlib
import time
from loguru import logger


__plugins__ = ['tax_forecast']

CACHE_FILENAME = 'api_cache.pkl'
CACHE_RETENTION_SECONDS = 24 * 60 * 60

class APIError(Exception):
    pass


def tax_forecast(entries, options, config_str):
    try:

        errors = []
        config = eval(config_str, {}, {})
        today = datetime.datetime.today().date()
        year = config.get("year")
        api_year = config.get("api_year")

        # get accounts (via open directives)
        accounts = {entry.account
                    for entry in entries
                    if isinstance(entry, data.Open)}

        taxable_accounts, deductable_accounts, asset_accounts, taxcredit_accounts = \
            get_accounts(config, accounts)

        # from accounts, aggregate positions
        taxable_incomes = get_income_expenses_from_accounts(
            entries, options, config, taxable_accounts)
        last_month_of_income = taxable_incomes.date.max().month
        taxable_incomes_by_currency = taxable_incomes.groupby(
            'currency').agg({'position': 'sum'}).reset_index()

        # add fx info
        taxable_incomes_by_currency = add_fx_info(entries,
                                                  options,
                                                  taxable_incomes_by_currency,
                                                  today)

        # aggregate and scale to 12 months
        taxable_income = taxable_incomes_by_currency.in_base_curr.sum() / \
            last_month_of_income*12

        assets = 0
        taxable_income_float = abs(float(taxable_income))
        witholding = 0
        municipality = config.get("municipality")
        marial_srtatus = config.get("marial_srtatus")
        n_children = config.get("n_children")

        # query tax calculator api
        url_staat = "/ZH-Web-Calculators/calculators/INCOME_ASSETS/calculate"
        url_bund = "/ZH-Web-Calculators/calculators/FEDERAL/calculate"

        data_staat = {
            "isLiabilityLessThanAYear": False,
            "hasTaxSeparation": False,
            "hasQualifiedInvestments": False,
            "taxYear": str(api_year),
            "liabilityBegin": None,
            "liabilityEnd": None,
            "name": "",
            "maritalStatus": str(marial_srtatus).lower(),
            "taxScale": "BASIC",
            "religionP1": "OTHERS",
            "religionP2": "OTHERS",
            "municipality": str(municipality),
            "taxableIncome": str(taxable_income_float),
            "ascertainedTaxableIncome": None,
            "qualifiedInvestmentsIncome": None,
            "taxableAssets": str(assets),
            "ascertainedTaxableAssets": None,
            "withholdingTax": str(witholding)
        }

        data_bund = {
            "isLiabilityLessThanAYearOrHasTaxSeparation": False,
            "taxYear": str(api_year),
            "name": "",
            "taxScale": str(marial_srtatus).upper(),
            "childrenNo": str(n_children),
            "taxableIncome": str(taxable_income_float),
            "ascertainedTaxableIncome": None,
        }
        try:
            response_staat = query_zh_tax_api(url_staat, data_staat)
            response_bund = query_zh_tax_api(url_bund, data_bund)
        except APIError:
            logger.info("could not fetch tax info from API. Not providing tax forecast")
            return entries, errors
        # extract relevant info and convert to monthly taxes
        taxes = {"Staats": response_staat.get('cantonalBaseTax').get('value'),
                 "Gemeinde": response_staat.get('municipalityTax').get('value'),
                 "Personal": response_staat.get('personalTax').get('value'),
                 "Vermoegen": response_staat.get('assetsTax').get('value'),
                 "Bundes": response_bund.get('totalFederalTax').get('value')
                 }

        precision = config.get("precision")
        taxes_per_month = {kind: Decimal(
            value/12).__round__(precision) for kind, value in taxes.items()}

        # create postings & transactions
        tax_transactions = make_transactions(config,
                                             options,
                                             taxes_per_month,
                                             last_month_of_income)

        entries.extend(tax_transactions)
    except Exception as e:
        logger.info(
            f"exception raised in tax_forecast plugin: {traceback.format_exc()}")
        errors.append(f"{traceback.format_exc()}\n{e}")
    return entries, errors


def load_cache():
    if os.path.exists(CACHE_FILENAME):
        with open(CACHE_FILENAME, 'rb') as f:
            cache = pickle.load(f)
        retention = time.time() - CACHE_RETENTION_SECONDS
        cache = {k: v for k, v in cache.items() if v['timestamp'] > retention}
        return cache
    return {}


def is_cache_entry_valid(entry_timestamp):
    current_timestamp = time.time()
    return (current_timestamp - entry_timestamp) < CACHE_RETENTION_SECONDS


def get_payload_hash(payload):
    return hashlib.md5(str(payload).encode()).hexdigest()


def save_cache(cache):
    with open(CACHE_FILENAME, 'wb') as f:
        pickle.dump(cache, f)


def query_zh_tax_api(url, data):
    cache = load_cache()
    payload_hash = get_payload_hash(data)

    # Check if the response for this payload is already cached and valid
    if payload_hash in cache:
        if is_cache_entry_valid(cache[payload_hash]['timestamp']):
            logger.info("Returning cached tax api response")
            return cache[payload_hash]['data']

    logger.info("payload not in cache, querying tax api")

    host = "webcalc.services.zh.ch"
    headers = {
        'Content-Type': 'application/json'
    }
    conn = httplib.HTTPSConnection(host)
    conn.request("POST", url, json.dumps(data), headers)
    response = conn.getresponse()

    if response.status == 200:
        logger.info('tax api query successful')

        answer = json.loads(response.read())
        cache[payload_hash] = {
            'data': answer,
            'timestamp': time.time()  # Record the current time as the timestamp of the cache entry
        }
        save_cache(cache)
        return answer
    else:
        logger.info("tax api was called but did not return 200")
        raise APIError("Tax API did not return 200 but {response.status} ")


def get_accounts(config, accounts):
    # Make a regex that matches if any of our regexes match for taxable accounts.
    if config['taxable_accounts']:
        combined = "(" + ")|(".join(config['taxable_accounts']) + ")"
        taxable_accounts = [acc for acc in accounts if bool(
            re.search(combined, acc, re.IGNORECASE))]
    else:
        taxable_accounts = []

    if config['deductable_accounts']:
        combined = "(" + ")|(".join(config['deductable_accounts']) + ")"
        deductable_accounts = [acc for acc in accounts if bool(
            re.search(combined, acc, re.IGNORECASE))]
    else:
        deductable_accounts = []

    asset_accounts = []  # Todo
    taxcredit_accounts = []  # todo

    return taxable_accounts, deductable_accounts, asset_accounts, taxcredit_accounts


def get_income_expenses_from_accounts(entries, options, config, taxable_accounts):
    # monthly aggregated data for all income and expense accounts
    year = config.get("year")
    dfs = []
    for acc in taxable_accounts:
        query = f'SELECT date, account, position, currency\nWHERE account = "{acc}"\nAND Year = {year}'
        result = run_query(entries, options, query, numberify=True)

        cols = [x[0] for x in result[0]]
        df = pd.DataFrame(result[1], columns=cols)
        df.columns = [
            'position' if 'position' in col else col for col in df.columns]
        dfs.append(df)

    return pd.concat(dfs).fillna(0)


def add_fx_info(entries, options, taxable_incomes_by_currency, today):
    # get forex rates
    fx_rates = []
    base_currency = options.get("operating_currency")[0]
    for currency in taxable_incomes_by_currency.currency:
        if currency == base_currency:
            fx_rates.append(1)
        else:
            prices = [e for e in entries if isinstance(e, data.Price)]
            prices_curr = [p for p in prices if p.currency == currency]
            best_date = min([p.date for p in prices_curr],
                            key=lambda x: abs(x - today))
            price = [p for p in prices_curr if p.date ==
                     best_date][0].amount.number
            fx_rates.append(price)

    # update the dataframe
    taxable_incomes_by_currency['fx_rate'] = fx_rates
    taxable_incomes_by_currency['in_base_curr'] = taxable_incomes_by_currency.position * \
        taxable_incomes_by_currency.fx_rate

    return taxable_incomes_by_currency


def make_transactions(config, options, taxes_per_month, last_month_of_income):
    year = config.get("year")
    base_currency = options.get("operating_currency")[0]
    tax_transactions = []
    day = config.get("tax_day_of_month")
    tax_base_account = config.get("tax_expenses_main_account")
    postings = [data.Posting(":".join([tax_base_account, tax_type]),
                             Amount(value, base_currency),
                             None, None, None, None)
                for tax_type, value in taxes_per_month.items()]

    postings.append(
        data.Posting(
            config.get("liability_account"),
            Amount(-sum([v for v in taxes_per_month.values()]),
                   base_currency),
            None, None, None, None))

    for month in range(1, last_month_of_income+1):
        month_name = datetime.date(1900, month, 1).strftime('%b')
        trans = data.Transaction(data.new_metadata(None, 0),
                                 datetime.date(year, month, day),
                                 '*',
                                 "Tax Authority",
                                 f"Tax forecast {month_name} {year}",
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings
                                 )
        tax_transactions.append(trans)
    return tax_transactions
