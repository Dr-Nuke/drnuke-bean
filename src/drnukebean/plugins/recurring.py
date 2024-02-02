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

import datetime
import pandas as pd

__plugins__ = ['recurring']


def recurring(entries, options_map):
    # converts a txn into recurring txns of same amount sum

    new_entries = []
    errors = []
    for entry in entries:
        if isinstance(entry, data.Transaction) and 'recurring_start' in entry.meta:
            start_date = entry.meta['recurring_start']
            frequency = entry.meta['recurring_frequency']
            times = int(entry.meta['recurring_times'])

            date_range = pd.date_range(start=start_date, periods=times, freq=frequency)

            # get list of amount values per posting. pay attentino to decimal rounding
            amounts = dict()
            for p in entry.postings:
                amount_orig = p.units.number
                splits = [round(amount_orig / times, 2) for i in range(times-1)]
                splits.append(amount_orig-sum(splits))
                amounts[p.account] = splits

            # prepare the meta
            dropkeys = ['recurring_start',
                    'recurring_frequency', 'recurring_frequency']
            meta = {key: val for key, val in entry.meta.items()
                    if key not in dropkeys}
            meta.update(
                {'recurring': f"split amounts into {times} chunks, {entry.meta['recurring_frequency']}, original txn date {entry.date.strftime(r'%Y-%m-%d')}"})
        

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
                for posting in entry.postings:
                    amount = amounts[posting.account][idx_date]
                    new_posting = posting._replace(units=posting.units._replace(number=amount))
                    new_txn.postings.append(new_posting)
                new_entries.append(new_txn)
        else:
            new_entries.append(entry)
    return new_entries, errors

