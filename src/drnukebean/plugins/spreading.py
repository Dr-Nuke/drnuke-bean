    # a beancount plugin to spread out transactions ofver a time period. I.e. spread
# out a yearly bill or account report over months.


from beancount.core.data import Transaction
from beancount.core import account_types
from beancount.parser import options
from beancount.core import data


from beancount.core.data import Transaction
from beancount.core.amount import Amount
from beancount.core import account as acc

import datetime
import pandas as pd 

#debug:
from beancount.parser import printer


__plugins__ = ['spreading']

def spreading(entries, options_map, config=None):
    new_entries = []
    errors = []


    for entry in entries:
        if isinstance(entry, Transaction) and 'p_spreading_start' in entry.meta:
            spread_entries, spread_errors = spread(entry)
            new_entries.extend(spread_entries)
            errors.extend(spread_errors)
        else:
            # Always replicate the existing entries - unless 'amortize_months'
            # is in the metadata
            new_entries.append(entry)



    return new_entries, errors

def spread(entry):
    # computes the speaded version of a transaction. 
    # i.e. distributing yearly PnL reports over the months
    
    # make emptly list of entries & errors
    entries = []
    errors = []
    
    # get info from incoming entry
    asset_posting = get_asset(entry)
    income_posting = get_income(entry)
    claim_account = 'Assets:Forderungen:' + acc.sans_root(income_posting.account)
    units = asset_posting.units
    value = units.number
    currency = units.currency
    amount = Amount(value,currency)

    # make claim posting for final entry
    claim_posting =data.Posting(account = claim_account,
                                units = -amount,
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
    splits = [round(value / n_divides,2) for i in range(n_divides-1)]
    splits.append(value-sum(splits))
    
    # make transactions
    for date,split in zip(dates,splits):
        
        amount = Amount(split,currency)
        # income leg
        pnl = data.Posting(account = income_posting.account,
                          units = -amount,
                          cost=None, price=None, flag=None, meta=None)
        claim = data.Posting(account = claim_account,
                          units = amount,
                          cost=None, price=None, flag=None, meta=None)
        
        meta = {key:val for key, val in entry.meta.items()}
        meta.update({'p_spreading': f"split {value} into {n_divides} chunks, {entry.meta['p_spreading_frequency']}"})
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

    return entries, errors

def get_income(entry):
    # returns the income/expense account of a transactino
    for post in entry.postings:
        if acc.root(1,post.account) in ['Income','Expenses']:
            return post
    raise Exception("entry did not have Income/ Expense posting") 
        
    
def get_asset(entry):
    # returns the asset/liability account of a transactino
    for post in entry.postings:
        if acc.root(1,post.account) in ['Assets','Liabilities']:
            return post
    raise Exception("entry did not have Asset/ Liability posting") 