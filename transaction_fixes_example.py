"""
transaction_fixes_example.py  --  stub fixes functions (copy to ~/ledger/transaction_fixes.py)

Each function receives a data.Transaction and returns a (possibly modified) Transaction.
The stubs below return the transaction unchanged — a valid starting point.

Extend with your own rules, e.g.:
    if 'Card payment to myStore' in txn.narration:
        txn = txn._replace(payee='myStore')
        txn = txn._replace(postings=[...])
    return txn

To rewrite narration/payee and tag on a metadata field for a matched transaction:
    if 'Card payment to myStore' in txn.narration:
        txn = txn._replace(narration='Groceries', payee='myStore')
        txn = txn._replace(meta={**txn.meta, 'category': 'groceries'})
    return txn

Cross-importer rules go in common_fixes(); call it first from each per-importer function.
"""

from beancount.core import data


def common_fixes(txn: data.Transaction) -> data.Transaction:
    """Rules that apply identically across all importers (e.g. well-known payees)."""
    return txn


def fixes_zkb(txn: data.Transaction) -> data.Transaction:
    txn = common_fixes(txn)
    return txn


def fixes_pfg(txn: data.Transaction) -> data.Transaction:
    txn = common_fixes(txn)
    return txn


def fixes_neon(txn: data.Transaction) -> data.Transaction:
    txn = common_fixes(txn)
    return txn


def fixes_revolut(txn: data.Transaction) -> data.Transaction:
    txn = common_fixes(txn)
    return txn


def fixes_ibkr(txn: data.Transaction) -> data.Transaction:
    txn = common_fixes(txn)
    return txn


def fixes_finpension(txn: data.Transaction) -> data.Transaction:
    txn = common_fixes(txn)
    return txn


def fixes_halbtax(txn: data.Transaction) -> data.Transaction:
    txn = common_fixes(txn)
    return txn
