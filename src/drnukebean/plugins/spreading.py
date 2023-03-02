"""
A beancount plugin to spread out transactions ofver a time period. I.e. spread
out a yearly bill or account report over months.

A transaction subject to this plugin needs to have
1) a income/expenses leg
2) a asset/liabilities leg
3) the following three meta-entries (with example values):
  p_spreading_frequency: "M"
  p_spreading_start: "2020-01-01"
  p_spreading_times: "12"
those meta values are direct input for pandas.date_range(), and can be used accordingly.

the plugin must be called with a parameter 'liability_acc_base':
plugin "drnukebean.plugins.spreading" "{'liability_acc_base': 'Assets:Liabilities:'}"
which is going to be the stem of the account that hosts the intermittendly spread-out balance


"""

from beancount.core import account as acc
from beancount.core import account_types
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.data import Transaction
from beancount.parser import options

import datetime
import pandas as pd


__plugins__ = ['spreading']




def spreading(entries, options_map, config_str):
    new_entries = []
    errors = []
    added_entries = [] # debug
    replaced_entries = []

    opened_accounts = [e.account for e in entries if isinstance(e,data.Open)]

    # assert correctness of the parameter
    config_obj = eval(config_str, {}, {})
    if not isinstance(config_obj, dict):
        errors.append(ConfigError(
            data.new_metadata('<spreading>', 0),
            "Invalid configuration for spreading plugin; skipping.", None))
        return entries, errors

    if not 'liability_acc_base' in config_obj:
        errors.append(ConfigError(
            data.new_metadata('<spreading>', 0),
            "spreading plugin: 'liability_acc_base' is missing in the paramters; skipping", None))
        return entries, errors

    for entry in entries:
        if isinstance(entry, Transaction) and 'p_spreading_start' in entry.meta:
            spread_entries, spread_errors, open_directive = spread(entry, config_obj)
            new_entries.extend(spread_entries)
            if open_directive.account not in opened_accounts:
                opened_accounts.append(open_directive.account)
                new_entries.append(open_directive)
                added_entries.append(open_directive)
            errors.extend(spread_errors)
            replaced_entries.append(entry)
            added_entries.extend(spread_entries)

        else:
            # Always replicate the existing entries - unless 'amortize_months'
            # is in the metadata
            new_entries.append(entry)

    return new_entries, errors


def spread(entry, config_obj):
    # computes the speaded version of a transaction.
    # i.e. distributing yearly PnL reports over the months

    # make emptly list of entries & errors
    entries = []
    errors = []

    # get info from incoming entry
    asset_posting = get_asset(entry)
    income_posting = get_income(entry)
    claim_account = config_obj['liability_acc_base'] + 'Spreading:' + \
        acc.sans_root(income_posting.account)
    open_directive = data.Open(entry.meta,
                               datetime.date(1970,1,1),
                               claim_account,
                               [income_posting.units.currency],
                               None)
    units = asset_posting.units
    value = units.number
    currency = units.currency
    amount = Amount(value, currency)

    # make claim posting for final entry
    claim_posting = data.Posting(account=claim_account,
                                 units=-amount,
                                 cost=None, price=None, flag=None, meta=None)

    # make final entry
    trans_orig = data.Transaction(meta=entry.meta,
                                  date=entry.date,
                                  flag=entry.flag,
                                  payee=entry.payee,
                                  narration=entry.narration,
                                  tags=entry.tags,
                                  links=entry.links,
                                  postings=[claim_posting,
                                            asset_posting])

    entries.append(trans_orig)

    # make spread-out transactions
    # number of divisions
    n_divides = int(entry.meta['p_spreading_times'])

    # list of dates
    dates = pd.date_range(entry.meta['p_spreading_start'],
                          periods=n_divides,
                          freq=entry.meta['p_spreading_frequency'])
    dates = [x.date() for x in list(dates)]

    # list of values. pay attentino to decimal rounding
    splits = [round(value / n_divides, 2) for i in range(n_divides-1)]
    splits.append(value-sum(splits))

    # make transactions
    for date, split in zip(dates, splits):

        amount = Amount(split, currency)
        # income leg
        pnl = data.Posting(account=income_posting.account,
                           units=-amount,
                           cost=None, price=None, flag=None, meta=None)
        claim = data.Posting(account=claim_account,
                             units=amount,
                             cost=None, price=None, flag=None, meta=None)

        dropkeys = ['p_spreading_times',
                    'p_spreading_start', 'p_spreading_frequency']
        meta = {key: val for key, val in entry.meta.items()
                if key not in dropkeys}
        meta.update(
            {'p_spreading': f"split {value} into {n_divides} chunks, {entry.meta['p_spreading_frequency']}, original date {entry.date.strftime(r'%Y-%m-%d')}"})
        trans = data.Transaction(meta=meta,
                                 date=date,
                                 flag='*',
                                 payee=entry.payee,
                                 narration=entry.narration,
                                 tags=entry.tags,
                                 links=entry.links,
                                 postings=[pnl,
                                           claim])
        entries.append(trans)

    return entries, errors, open_directive


def get_income(entry):
    # returns the income/expense account of a transactino
    for post in entry.postings:
        if acc.root(1, post.account) in ['Income', 'Expenses']:
            return post
    raise Exception(f"entry did not have Income/ Expense posting: {entry}")


def get_asset(entry):
    # returns the asset/liability account of a transactino
    for post in entry.postings:
        if acc.root(1, post.account) in ['Assets', 'Liabilities']:
            return post
    raise Exception(f"entry did not have Asset/ Liability posting: {entry}")
