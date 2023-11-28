"""
A beancount plugin for regular budgeting transactions

"""

from beancount.core import account as acc
from beancount.core import account_types
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.data import Transaction
from beancount.parser import options

import datetime
import pandas as pd
from copy import deepcopy


__plugins__ = ['budgeting']

NOW = datetime.datetime.now()


def budgeting(entries, options_map, config_str):
    new_entries = []
    errors = []

    # assert correctness of the parameter
    config_obj = eval(config_str, {}, {})
    if not isinstance(config_obj, dict):
        errors.append(ConfigError(
            data.new_metadata('<spreading>', 0),
            "Invalid configuration for budgeting plugin; skipping.", None))
        return entries, errors

    for entry in entries:
        if isinstance(entry, Transaction) and 'p_budgeting_start' in entry.meta:
            spread_entries, spread_errors = budget(entry, config_obj)
            new_entries.extend(spread_entries)
            errors.extend(spread_errors)

        else:
            new_entries.append(entry)

    return new_entries, errors


def budget(entry, config_obj):
    # computes the speaded version of a transaction.
    # i.e. distributing yearly PnL reports over the months

    # make emptly list of entries & errors
    entries = []
    errors = []

    # make spread-out transactions
    start = entry.meta['p_budgeting_start']
    freq = entry.meta['p_budgeting_frequency']
    repeats = int(entry.meta['p_budgeting_times'])
    limit = entry.meta.get('p_budgeting_limit_to_today')

    # list of dates
    dates = pd.date_range(start = start,
                          periods=repeats,
                          freq=freq)
    
    if limit and limit=='False':
        pass
    else:
        dates = [x.date() for x in dates if x<NOW]

    dropkeys = ['p_budgeting_start',
                'p_budgeting_frequency', 'p_budgeting_times']
    meta = {key: val for key, val in entry.meta.items()
            if key not in dropkeys}
    fname= entry.meta.get('filename').split('/')[-1]
    line_no = entry.meta.get('lineno')
    entry_date = entry.date.strftime(r'%Y-%m-%d')
    meta.update(
        {'p_budgeting': f"budget plugin generated entry. Original transaction in {fname} line {line_no} at {entry_date}"})
    
    # make transactions
    for date in dates:
        new_entry = entry._replace(meta=meta, date = date)
        entries.append(new_entry)

    return entries, errors
