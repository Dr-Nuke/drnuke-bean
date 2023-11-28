# a beancount plugin that creplaces amounts of poytings by a fraction
# according to partner splits

"""
A beancount plugin for regular budgeting transactions

"""

from beancount.core import account as acc
from beancount.core import account_types
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.data import Transaction
from beancount.parser import options
from beancount.core.number import Decimal

import datetime
import pandas as pd
from copy import deepcopy


__plugins__ = ['partner']


def partner(entries, options_map):
    return_entries = []
    errors = []

    for entry in entries:
        if "partner" in entry.meta:
            partner_entries, partner_errors = apply_partner(entry)
            return_entries.extend(partner_entries)
            errors.extend(partner_errors)

        else:
            return_entries.append(entry)

    return return_entries, errors


def apply_partner(entry):
    # computes the partner-version of an entry

    # make emptly list of entries & errors
    entries = []
    errors = []

    factor = entry.meta.get("partner")
    if factor is None:
        factor = Decimal("0.5")  # default value

    # make transactions
    new_postings = []
    for pos in entry.postings:
        new_number = round(pos.units.number*factor, 2)
        units = Amount(new_number, pos.units.currency)
        new_postings.append(pos._replace(units=units))

    entries.append(entry._replace(postings=new_postings))
    return entries, errors
