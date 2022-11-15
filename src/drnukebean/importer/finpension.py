"""
This is a beancount importer for Finpension. 
Setup:
1) have a running beancount system
2) run 'bean-extract config.py path/to/finpension/transaction_report.csv -f mainLedgerFile.bean
"""

import pandas as pd
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import warnings
import pickle
import re
import numpy as np
import sys
from loguru import logger

import yaml
from os import path

from beancount.query import query
from beancount.parser import options
from beancount.ingest import importer
from beancount.core import data, amount
from beancount.core.number import D
from beancount.core.number import Decimal
from beancount.core import position
from beancount.core.number import MISSING

logger.add("sys.stderr",
           format="{time}|{name}|{level}|{module}:{file}:{line}|{message}",
           level="INFO",
           colorize=True,
           rotation="1 week",
           backtrace=True,
           diagnose=True,
           )


class FinPensionImporter(importer.ImporterProtocol):
    """
    Beancount Importer for Finpension
    """

    def __init__(self,
                 Mainaccount=None,  # for example Assets:Invest:IB
                 currency='CHF',
                 divSuffix='Div',  # suffix for dividend Account , like Assets:Invest:IB:VT:Div
                 interestSuffix='Interest',
                 WHTAccount=None,
                 FeesSuffix='Fees',
                 PnLSuffix='PnL',
                 depositAccount='',
                 ISIN_lookup=None,
                 file_encoding="utf-8-sig",
                 sep=";",
                 # a regex pattern that allows to distinguish between pillar 2&3 and individual portfolios
                 regex=r"finpension_(S[2,3][a]?)_(Portfolio\d)",
                 ):

        self.Mainaccount = Mainaccount  # main IB account in beancount
        self.currency = currency        # main currency of IB account
        self.divSuffix = divSuffix
        self.WHTAccount = WHTAccount
        self.interestSuffix = interestSuffix
        self.FeesSuffix = FeesSuffix
        self.PnLSuffix = PnLSuffix
        self.ISIN_lookup = ISIN_lookup
        # Cash deposits are usually already covered
        self.depositAccount = depositAccount
        # by checkings account statements. If you want anyway the
        # deposit transactions, provide a True value
        self.file_encoding = file_encoding
        self.flag = '*'
        self.regex = regex
        self.sep = sep

    def identify(self, file):
        # intended file format is *finpension_s2_p1* for säule(pillar) 2 portfolio 1
        result = bool(re.search(self.regex, file.name, re.IGNORECASE))
        logger.info(
            f"identify assertion for finpension importer and file '{file.name}': {result}")
        return result

    def getLiquidityAccount(self, currency):
        return ':'.join([self.Mainaccount, currency])

    def getDivIncomeAcconut(self, currency, symbol):
        return ':'.join([self.Mainaccount.replace('Assets', 'Income'), symbol, self.divSuffix])

    def getInterestIncomeAcconut(self, currency):
        return ':'.join([self.Mainaccount.replace('Assets', 'Income'), self.interestSuffix, currency])

    def getAssetAccount(self, symbol):
        return ':'.join([self.Mainaccount, symbol])

    def getFeesAccount(self, currency):
        return ':'.join([self.Mainaccount.replace('Assets', 'Expenses'), self.FeesSuffix, currency])

    def getPNLAccount(self, symbol):
        return ':'.join([self.Mainaccount.replace('Assets', 'Income'), symbol, self.PnLSuffix])

    def file_account(self, _):
        return self.account

    def fix_accounts(self, file):
        try:
            pillar, portfolio = re.search(
                self.regex, file.name, re.IGNORECASE).groups()
        except AttributeError as e:
            logger.error(
                f"could not extract pillar and/or portfolio from filename {file.name} with regex pattern {self.regex}.")
            raise AttributeError(e)
        self.Mainaccount = ":".join(
            [re.sub(r"S\d", pillar, self.Mainaccount), portfolio])


    def extract(self, file_, existing_entries=None):
        # the actual processing of the csv export

        # fix Account names with regard to pillar 2/3 and different portfolios.
        self.fix_accounts(file_)

        df = pd.read_csv(file_.name,
                         sep=self.sep,
                         )
        # convert specific columns to Decimal with specific precisions
        to_decimal_dict = {"Number of Shares": 3,
                           "Asset Price in CHF": 2,
                           "Cash Flow": 2,
                           "Balance": 2}
        for col, digits in to_decimal_dict.items():
            df[col] = df[col].apply(lambda x: Decimal(x).__round__(digits))

        df['Date'] = pd.to_datetime(df['Date']).apply(datetime.date)

        # disect the complete report in similar transactions
        trades = df[df.Category == "Portfolio Transaction"]
        transfers = df[df.Category == "Transfer vested benefits"]
        fees = df[df.Category == "Implementation fees"]
        interests = df[df.Category == "Interests"]
        dividends = df[df.Category == "Dividend and Interest Distributions"]

        return_txn = (self.Trades(trades)
                      # + self.Transfers(transfers) # omitted for only occuring very rarely
                      + self.Fees(fees)
                      + self.Interest(interests)
                      + self.Dividends(dividends)
                      + self.Balances(df)
                      )

        return return_txn

    def Trades(self, trades):
        bean_transactions = []
        for idx, row in trades.iterrows():
            currency = row['Asset Currency']
            isin = row['ISIN']
            symbol = self.ISIN_lookup.get(isin)
            asset = row['Asset Name']
            if symbol is None:
                logger.error(
                    f"Could not fetch isin {row['symbol']} from supplied ISINs {list(self.ISIN_lookup.keys())}")
                continue
            proceeds = amount.Amount(row['Cash Flow'], currency)

            quantity = amount.Amount(row['Number of Shares'], symbol)
            price = amount.Amount(row['Asset Price in CHF'], "CHF")

            cost = position.CostSpec(
                number_per=price.number,
                number_total=None,
                currency=currency,
                date=None,
                label=None,
                merge=False)

            postings = [
                data.Posting(self.getAssetAccount(symbol),
                             quantity, cost, None, None, None),
                data.Posting(self.getLiquidityAccount(currency),
                             proceeds, None, None, None, None),
            ]
            if quantity.number > 0:
                buy_sell = "BUY"
            else:
                buy_sell = "SELL"
            bean_transactions.append(
                data.Transaction(data.new_metadata('Buy', 0),
                                 row['Date'],
                                 self.flag,
                                 isin,     # payee
                                 ' '.join(
                                     [buy_sell, quantity.to_string(), '@', price.to_string()+";", asset]),
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings
                                 ))
        return bean_transactions


    def Fees(self, fees):

        bean_transactions = []
        for idx, row in fees.iterrows():
            currency = row['Asset Currency']
            amount_ = amount.Amount(row['Cash Flow'], currency)

            # make the postings, two for fees
            postings = [data.Posting(self.getFeesAccount(currency),
                                     -amount_, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                     amount_, None, None, None, None)]
            meta = data.new_metadata(__file__, 0, {})  # actually no metadata
            bean_transactions.append(
                data.Transaction(meta,
                                 row['Date'],
                                 self.flag,
                                 'FinPension',     # payee
                                 "Fees",
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings))
        return bean_transactions

    def Dividends(self, dividends):
        # this function crates Dividend transactions from IBKR data
        # make dividend & WHT transactions

        bean_transactions = []
        for idx, row in dividends.iterrows():
            currency = row['Asset Currency']
            isin = row['ISIN']
            symbol = self.ISIN_lookup.get(isin)
            if symbol is None:
                logger.error(
                    f"Could not fetch isin {row['symbol']} from supplied ISINs {list(self.ISIN_lookup.keys())}")
                continue
            amount_div = amount.Amount(row['Cash Flow'], currency)
            pershare = amount.Amount(row['Asset Price in CHF'], currency)

            postings = [data.Posting(self.getDivIncomeAcconut(currency, symbol),
                                     -amount_div, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                     amount_div, None, None, None, None)
                        ]
            meta = data.new_metadata(
                'dividend', 0, {'isin': isin, 'per_share': pershare})
            bean_transactions.append(
                data.Transaction(meta,  # could add div per share, ISIN,....
                                 row['Date'],
                                 self.flag,
                                 isin,     # payee
                                 f"Dividend {symbol}; {row['Asset Name']}",
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings
                                 ))

        return bean_transactions

    def Interest(self, int_):
        # calculates interest payments from IBKR data
        bean_transactions = []
        for idx, row in int_.iterrows():
            currency = row['Asset Currency']
            amount_ = amount.Amount(row['Cash Flow'], currency)

            # make the postings, two for interest payments
            # received and paid interests are booked on the same account
            postings = [data.Posting(self.getInterestIncomeAcconut(currency),
                                     -amount_, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                     amount_, None, None, None, None)
                        ]
            meta = data.new_metadata('Interest', 0)
            bean_transactions.append(
                data.Transaction(meta,  # could add div per share, ISIN,....
                                 row['Date'],
                                 self.flag,
                                 'FinPension',     # payee
                                 "Interest",
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings
                                 ))
        return bean_transactions

    def Balances(self, df):
        # generate Balance statements for every latest transaction
        # (there may be multiple, no idea how to pick the right one)
        # simply make a balance for all values, the correct one should be one of them

        bean_transactions = []
        df = df[df['Date'] == df['Date'].max()]
        for idx, row in df.iterrows():
            currency = row['Asset Currency']
            amount_ = amount.Amount(row['Balance'], currency)
            meta = data.new_metadata('balance', 0)
            bean_transactions.append(data.Balance(
                meta,
                row['Date'] + timedelta(days=1),  # see tariochtools EC imp.
                self.getLiquidityAccount(currency),
                amount_,
                None,
                None))
        return bean_transactions
