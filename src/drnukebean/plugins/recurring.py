"""
A beancount plugin to create repeatedly the same transaction, 
e.g. the monthly tax liability before the tax bill is issued.

A transaction subject to this plugin needs to have
1) a income/expenses leg
2) a asset/liabilities leg
3) the following three meta-entries (with example values):
  recurring_frequency: "M"
  recurring_start: "2020-01-01"
  recurring_times: "12"
those meta values are direct input for pandas.date_range(), and can be used accordingly.

the plugin must be called with no parameter: 
plugin "drnukebean.plugins.recurring" 
"""

from beancount.core import account as acc
from beancount.core import account_types
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.data import Transaction
from beancount.parser import options
from decimal import Decimal

import datetime
import pandas as pd
import io
from contextlib import redirect_stdout

__plugins__ = ['recurring']


def recurring(entries, options_map, config_str):
    errors = []
    new_entries = []

    for entry in entries:
        if isinstance(entry, data.Transaction) and 'recurring_start' in entry.meta:
            start_date = entry.meta['recurring_start']
            frequency = entry.meta['recurring_frequency']
            times = int(entry.meta['recurring_times'])

            date_range = pd.date_range(start=start_date, periods=times, freq=frequency)

            # Create a unique key for each posting by combining account and index
            amounts = []
            for idx, p in enumerate(entry.postings):
                amount_orig = p.units.number
                splits = [round(amount_orig / times, 2) for i in range(times - 1)]
                splits.append(amount_orig - sum(splits))
                amounts.append((idx, p.account, splits))

            # Correct for rounding errors
            rounding_errors = [0] * times
            for _, _, splits in amounts:
                for i, split in enumerate(splits):
                    rounding_errors[i] += split

            if any(rounding_errors):
                last_posting_idx = amounts[-1][0]
                last_posting_splits = amounts[-1][2]
                amounts[-1] = (last_posting_idx, amounts[-1][1], [value - rounding_errors[i] for i, value in enumerate(last_posting_splits)])

            # Prepare the meta
            dropkeys = ['recurring_start', 'recurring_frequency', 'recurring_times']
            meta = {key: val for key, val in entry.meta.items() if key not in dropkeys}
            meta.update({'recurring': f"split amounts into {times} chunks, {entry.meta['recurring_frequency']}, original txn date {entry.date.strftime(r'%Y-%m-%d')}"})

            for idx_date, new_date in enumerate(date_range):
                new_txn = data.Transaction(
                    meta=entry.meta,
                    date=new_date.date(),
                    flag=entry.flag,
                    payee=entry.payee,
                    narration=entry.narration,
                    tags=entry.tags,
                    links=entry.links,
                    postings=[]
                )
                for idx, account, splits in amounts:
                    amount = splits[idx_date]
                    posting = entry.postings[idx]
                    new_posting = posting._replace(units=posting.units._replace(number=amount))
                    new_txn.postings.append(new_posting)
                new_entries.append(new_txn)
        else:
            new_entries.append(entry)
    return new_entries, errors