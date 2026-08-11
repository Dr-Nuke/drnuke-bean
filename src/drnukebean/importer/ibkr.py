"""
Beancount importer for Interactive Brokers (IBKR) FlexQuery XML reports.

This module is a pure file parser.  Network access lives in
:mod:`drnukebean.importer.ibkr_flexquery`.

Identification
--------------
Files are matched by XML content: the root element must be
``<FlexQueryResponse>`` and, when *query_name* is supplied, its
``queryName`` attribute must match exactly.  This removes any dependency on
filenames or IBKR account numbers.

Transaction types handled
-------------------------
Trades
  * Stock buy / sell (with closed-lot cost specs for sells; when
    *transactionID_labeled_since* is set, lots acquired on or after that date
    get a CostSpec label equal to the IBKR ``transactionID`` for exact lot
    matching)
  * Forex buy / sell

CashTransactions
  * Dividends and payments-in-lieu, with optional withholding-tax matching
  * Return-of-capital distributions (Swiss-specific)
  * Interest received / paid, with optional withholding-tax
  * Broker fees
  * Cash deposits / withdrawals (optional -- suppressed when *deposit_account*
    is empty)

Balances
  * One ``data.Balance`` per non-summary ``CashReport`` row

FlexQuery sections required
---------------------------
CashReport        : Currency, EndingCash, ToDate
Trades            : Symbol, Currency, Quantity, TradePrice, Proceeds,
                    IBCommission, IBCommissionCurrency, NetCash,
                    DateTime, TradeDate, OpenDateTime, TransactionID,
                    BuySell, LevelOfDetail
CashTransactions  : Symbol, Currency, Amount, Type, Description,
                    ReportDate, DateTime
Transfers         : Symbol, Currency, Amount, Type, Description,
                    ReportDate, Quantity, CashTransfer, Account

Usage in run_imports.py::

    from drnukebean.importer.ibkr import IBKRImporter
    from drnukebean.importer.ibkr_flexquery import make_ibkr_setup

    _ibkr_dir = Path('~/downloads/ibkr').expanduser()

    pipelines = [
        dict(
            name='ibkr',
            importer=IBKRImporter(
                account='Assets:Invest:IBKR',
                query_name=cfg.IBKR_QUERY_NAME,
                currency='CHF',
                # date of the first import run with lot labeling; never change
                transactionID_labeled_since=date(2026, 8, 1),
                account_map={
                    'U88776655': {
                        'root': 'Assets:Invest:IBKR:Trading',
                        'deposit_from': 'Assets:Bank:ZKB:CHF',
                    },
                    'U99887766': {
                        'root': 'Assets:Invest:IBKR:Pension',
                        # no deposit_from -> deposit/withdrawal emitted with ! flag
                    },
                },
            ),
            source_dir=_ibkr_dir,
            bean_output_file='~/ledger/import/IBKR.bean',
            setup=make_ibkr_setup(
                token=cfg.IBKR_TOKEN,
                query_id=cfg.IBKR_QUERY_ID,
                query_name=cfg.IBKR_QUERY_NAME,
                dest_dir=_ibkr_dir,
            ),
            fixes=fixes_ibkr,
            predict=False,
        ),
    ]

    When *account_map* is omitted all statements are posted under *account*
    (single-account mode).  When provided, every ``FlexStatement`` account ID
    must be present in the map -- an unlisted ID raises ``RuntimeError``
    immediately to prevent silent mis-posting.

    Deposit / withdrawal counterpart account
    -----------------------------------------
    The source or destination of a cash transfer is unknown to the importer.
    Configure ``deposit_from`` per account in *account_map* to supply the
    counterpart.  When omitted the transaction is emitted with a single posting
    (the IBKR liquidity leg) and flag ``!`` so it is visible in ``bean-check``
    output and requires manual completion.
"""

from __future__ import annotations

import functools
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import beangulp
from beancount.core import amount, data, position
from beancount.core.number import D
from ibflex import Types, parser
from ibflex.enums import BuySell, CashAction
from loguru import logger

from drnukebean.importer.util import amount_add, minus

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_ROC_STR = "Return of Capital"  # Swiss-specific: dividends that are legally RoC
_FLEX_ROOT_TAG = "FlexQueryResponse"

# Compiled regexes -- reused across all rows
_RE_WHT_PIL = re.compile(r".*payment in lieu of dividend", re.IGNORECASE)
_RE_WHT_DIV = re.compile(r".*dividend", re.IGNORECASE)
_RE_WHT_INTEREST = re.compile(r".*on credit int", re.IGNORECASE)
_RE_ISIN = re.compile(r"\(([A-Z]{2}[A-Z0-9]{9}\d)\)")
_RE_PER_SHARE = re.compile(r"(?P<amount>\d*[.]\d*)\s*(?:[A-Z]+\s*)?PER SHARE", re.IGNORECASE)
_RE_FEE_MONTH = re.compile(r"\b\w{3}\s+\d{4}\b")

# ---------------------------------------------------------------------------
# Module-level cached parser  (shared across instances, keyed by filepath)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def _parse_flex_file(filepath: str) -> Types.FlexQueryResponse:
    """Parse and cache a FlexQuery XML file.  Avoids re-parsing within a run."""
    raw = Path(filepath).read_bytes()
    statement = parser.parse(raw)
    if not isinstance(statement, Types.FlexQueryResponse):
        raise ValueError(f"Unexpected ibflex response type: {type(statement)}")
    return statement


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _is_forex(symbol: str) -> bool:
    """Return True for IBKR forex pair symbols such as ``USD.CHF``."""
    return bool(re.match(r"^[A-Z]{3}\.[A-Z]{3}$", symbol))


def _wht_div_type(description: str) -> CashAction | str | None:
    """Classify a WHT description and return the matching dividend type.

    Returns:
        ``CashAction.PAYMENTINLIEU``  -- WHT on a payment-in-lieu dividend
        ``CashAction.DIVIDEND``       -- WHT on a regular dividend
        ``"interest"``                -- WHT on interest income
        ``None``                      -- unrecognised description
    """
    if _RE_WHT_PIL.match(description):
        return CashAction.PAYMENTINLIEU
    if _RE_WHT_DIV.match(description):
        return CashAction.DIVIDEND
    if _RE_WHT_INTEREST.match(description):
        return "interest"
    return None


def _forex_currencies(symbol: str) -> tuple[str, str]:
    """Return ``(primary, secondary)`` from a forex symbol like ``USD.CHF``."""
    m = re.match(r"(?P<prim>[A-Z]{3})\.(?P<sec>[A-Z]{3})", symbol)
    if not m:
        raise ValueError(f"Not a forex symbol: {symbol!r}")
    return m.group("prim"), m.group("sec")


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class IBKRImporter(beangulp.Importer):
    """Beancount importer for IBKR FlexQuery XML reports.

    Args:
        account:            Default beancount account root, e.g.
                            ``Assets:Invest:IBKR``.  Used as-is when
                            *account_map* is ``None`` (single-account mode),
                            and as the fallback prefix for unmapped account IDs
                            when *account_map* is provided.
        query_name:         Expected ``queryName`` attribute in the
                            ``FlexQueryResponse`` root element.  Used to
                            identify files and to disambiguate when multiple
                            XML files are present.  If ``None``, any
                            FlexQueryResponse XML file matches.
        currency:           Base currency of the IB account (default ``CHF``).
        account_map:        Optional mapping of IBKR account IDs to per-account
                            configuration dicts.  Required key: ``root`` (the
                            beancount account root, e.g.
                            ``'Assets:Invest:IBKR:Trading'``).  Optional key:
                            ``deposit_from`` (counterpart account for cash
                            deposits / withdrawals; omit to emit a single-leg
                            ``!`` transaction).  When ``None`` (default) all
                            statements are posted under *account*
                            (single-account mode); deposits get ``!`` flag.
        asset_suffix:       Explicit asset suffix.  Overrides the derived
                            ``<Account root>:<symbol>`` with
                            ``<Account root>:<asset_suffix>``.
        div_suffix:         Sub-account suffix for dividend income
                            (default ``Div``).
        div_account:        Explicit dividend income account.  Overrides the
                            derived ``<Income root>:<symbol>:<div_suffix>``.
        interest_suffix:    Sub-account suffix for interest income
                            (default ``Interest``).
        wht_account:        Root account for withholding-tax expenses, e.g.
                            ``Expenses:Invest:IBKR:WHT``.  Symbol or currency
                            is appended as a further sub-account.
        fees_suffix:        Sub-account suffix for fees (default ``Fees``).
        fees_account:       Explicit fees account.  Overrides the derived
                            account.
        pnl_suffix:         Sub-account suffix for realised P&L
                            (default ``PnL``).
        transactionID_labeled_since:
                            Acquisition-date threshold for exact lot matching.
                            Lots acquired on or after this date (BUY:
                            ``tradeDate``; SELL closed lot:
                            ``openDateTime.date()``) get a CostSpec label equal
                            to the IBKR ``transactionID``, so a sell reduces
                            exactly the lot IBKR reports as closed (same-day
                            multi-lot sells are otherwise ambiguous).  Labeled
                            BUY postings carry no basis number; beancount
                            interpolates it from the cash legs.  Lots acquired
                            before the threshold keep the previous shape
                            (priced BUY cost spec, date-only sell cost spec)
                            and carry the transactionID as posting metadata
                            instead, for a later scripted ledger migration.
                            Must be a ``datetime.date`` (a ``datetime.datetime``
                            is rejected).  Set it once -- to the date of the
                            first import run with this importer version --
                            and never change it afterwards: moving
                            it backwards labels reductions of unlabeled
                            historical lots, moving it forwards un-labels
                            reductions of already-labeled lots.  ``None``
                            (default) disables labeling and logs a warning.
        symbol_map:         Dict remapping IBKR symbols to beancount commodity
                            names, e.g. ``{'VWRL': 'VWRL3'}``.  Applied after
                            auto-cleanup.
    """

    def __init__(
        self,
        account: str,
        query_name: str | None = None,
        currency: str = "CHF",
        account_map: dict[str, dict[str, str]] | None = None,
        asset_suffix: str | None = None,
        div_suffix: str = "Div",
        div_account: str | None = None,
        interest_suffix: str = "Interest",
        wht_account: str | None = None,
        fees_suffix: str = "Fees",
        fees_account: str | None = None,
        pnl_suffix: str = "PnL",
        transactionID_labeled_since: date | None = None,
        symbol_map: dict[str, str] | None = None,
    ) -> None:
        if account_map is not None:
            for acct_id, cfg in account_map.items():
                if "root" not in cfg:
                    raise ValueError(f"account_map[{acct_id!r}] missing required key 'root'")
                unknown = set(cfg) - {"root", "deposit_from"}
                if unknown:
                    raise ValueError(
                        f"account_map[{acct_id!r}] has unknown keys: {sorted(unknown)}"
                    )
        self._account = account
        self._query_name = query_name
        self._currency = currency
        self._account_map = account_map
        self._asset_suffix = asset_suffix
        self._div_suffix = div_suffix
        self._div_account = div_account
        self._interest_suffix = interest_suffix
        self._wht_account = wht_account
        self._fees_suffix = fees_suffix
        self._fees_account = fees_account
        self._pnl_suffix = pnl_suffix
        if (
            transactionID_labeled_since is not None
            and type(transactionID_labeled_since) is not date
        ):
            raise TypeError(
                "IBKRImporter: transactionID_labeled_since must be a datetime.date "
                f"(not {type(transactionID_labeled_since).__name__}); "
                f"got {transactionID_labeled_since!r}"
            )
        self._labeled_since: date | None = transactionID_labeled_since
        if self._labeled_since is None:
            logger.warning(
                "IBKRImporter: transactionID_labeled_since is not set -- lot labels are "
                "disabled and same-day multi-lot sells may silently match the wrong lot. "
                "Set transactionID_labeled_since in the importer config to enable exact "
                "lot matching."
            )
        self._symbol_map: dict[str, str] = symbol_map or {}

    # ------------------------------------------------------------------
    # beangulp protocol
    # ------------------------------------------------------------------

    def identify(self, filepath: str) -> bool:
        """Match IBKR FlexQuery XML files, optionally filtered by query name."""
        try:
            # Own bank's FlexQuery export, not untrusted input.
            root = ET.parse(filepath).getroot()  # noqa: S314
        except ET.ParseError:
            return False
        if root.tag != _FLEX_ROOT_TAG:
            return False
        if self._query_name:
            return root.get("queryName") == self._query_name
        return True

    def account(self, filepath: str) -> str:
        return self._account

    def date(self, filepath: str) -> date | None:
        """Return the report closing date from the first non-summary CashReport row."""
        try:
            statement = _parse_flex_file(filepath)
        except Exception:
            return None
        for stmt in statement.FlexStatements:
            for cr in stmt.CashReport:
                if str(cr.currency) != "BASE_SUMMARY":
                    return cr.toDate
        return None

    def extract(self, filepath: str, existing: list) -> list:
        """Parse the FlexQuery XML and return beancount directives."""
        try:
            statement = _parse_flex_file(filepath)
        except Exception as exc:
            logger.error("IBKRImporter: failed to parse {}: {}", filepath, exc)
            return []

        entries: list = []
        for stmt in statement.FlexStatements:
            account_root = self._resolve_account(stmt.accountId)
            deposit_from = self._resolve_deposit_from(stmt.accountId)
            entries.extend(self._trades(stmt.Trades, filepath, account_root))
            entries.extend(
                self._cash_transactions(stmt.CashTransactions, filepath, account_root, deposit_from)
            )
            entries.extend(
                self._transfers(stmt.Transfers, filepath, account_root)
            )
            entries.extend(self._balances(stmt.CashReport, account_root))
        return entries

    # ------------------------------------------------------------------
    # Account resolution
    # ------------------------------------------------------------------

    def _resolve_account(self, account_id: str) -> str:
        """Return the beancount account root for a given IBKR account ID.

        When *account_map* is ``None`` (single-account mode) the configured
        *account* is returned unchanged for every statement.  When
        *account_map* is provided the ID must be present in the map; an
        unmapped ID raises ``RuntimeError`` to prevent silently posting
        transactions to a wrong or auto-generated account.
        """
        if self._account_map is None:
            return self._account
        if account_id not in self._account_map:
            raise RuntimeError(
                f"IBKRImporter: account ID {account_id!r} not found in account_map. "
                f"Known IDs: {sorted(self._account_map)}. "
                f"Add it to the account_map in run_imports.py."
            )
        return self._account_map[account_id]["root"]

    def _resolve_deposit_from(self, account_id: str) -> str | None:
        """Return the deposit counterpart account for a given IBKR account ID, or None.

        Returns ``None`` when *account_map* is not set or when ``deposit_from``
        is absent for the account.  A ``None`` result causes ``_deposit`` to
        emit a single-leg transaction with flag ``!``.
        """
        if self._account_map is None:
            return None
        return self._account_map.get(account_id, {}).get("deposit_from")

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def _trades(self, trades, filepath: str, account_root: str) -> list:
        """Dispatch trade rows to forex or stock handlers."""
        all_trades = list(trades)
        forex_trades = [(i, t) for i, t in enumerate(all_trades) if _is_forex(t.symbol)]
        stock_trades = [(i, t) for i, t in enumerate(all_trades) if not _is_forex(t.symbol)]

        entries: list = []
        for _, trx in forex_trades:
            try:
                entries.append(self._forex_trade(trx, filepath, account_root))
            except Exception as exc:
                logger.warning("IBKRImporter: skipping forex trade {}: {}", trx.symbol, exc)

        entries.extend(self._stock_trades(stock_trades, filepath, account_root))
        return entries

    def _forex_trade(self, trx, filepath: str, account_root: str) -> data.Transaction:
        curr_prim, curr_sec = _forex_currencies(trx.symbol)
        quantity = amount.Amount(round(trx.quantity, 2), curr_prim)
        proceeds = amount.Amount(round(trx.proceeds, 2), curr_sec)
        price = amount.Amount(trx.tradePrice, curr_sec)
        commission = amount.Amount(round(trx.ibCommission, 2), trx.ibCommissionCurrency)
        buysell = trx.buySell.name

        postings = [
            data.Posting(
                self._liquidity_account(curr_prim, account_root), quantity, None, price, None, None
            ),
            data.Posting(
                self._liquidity_account(curr_sec, account_root), proceeds, None, None, None, None
            ),
            data.Posting(
                self._liquidity_account(trx.ibCommissionCurrency, account_root),
                commission,
                None,
                None,
                None,
                None,
            ),
            data.Posting(
                self._fees_account_name(trx.ibCommissionCurrency, account_root),
                minus(commission),
                None,
                None,
                None,
                None,
            ),
        ]
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.tradeDate,
            "*",
            trx.symbol,
            f"{buysell} {quantity.to_string()} @ {price.to_string()}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _stock_trades(self, indexed_trades: list, filepath: str, account_root: str) -> list:
        """Process stock execution trades; match closed lots to each sell."""
        executions = [(i, t) for i, t in indexed_trades if t.levelOfDetail == "EXECUTION"]
        closed_lots = [(i, t) for i, t in indexed_trades if t.levelOfDetail == "CLOSED_LOT"]

        entries: list = []
        for exec_idx, trx in executions:
            try:
                if trx.buySell in (BuySell.BUY, BuySell.CANCELBUY):
                    entries.append(self._buy_shares(trx, filepath, account_root))
                elif trx.buySell in (BuySell.SELL, BuySell.CANCELSELL):
                    my_lots = [
                        lot
                        for lot_idx, lot in closed_lots
                        if lot.symbol == trx.symbol and lot_idx > exec_idx
                    ]
                    entries.append(self._sell_shares(trx, my_lots, filepath, account_root))
            except Exception as exc:
                logger.warning(
                    "IBKRImporter: skipping stock trade {} on {}: {}", trx.symbol, trx.dateTime, exc
                )
        return entries

    def _buy_cost_spec(self, trx) -> tuple[position.CostSpec, dict | None]:
        """CostSpec + posting meta for an augmenting BUY posting."""
        return self._cost_spec_and_meta(
            transaction_id=trx.transactionID,
            acq_date=trx.tradeDate,
            currency=trx.currency,
            # unlabeled BUYs keep the asserted raw-price basis
            unlabeled_number_per=trx.tradePrice,
            context=f"BUY {trx.symbol} {trx.tradeDate}",
        )

    def _sell_cost_spec(self, lot) -> tuple[position.CostSpec, dict | None]:
        """CostSpec + posting meta for a reducing SELL (closed-lot) posting."""
        return self._cost_spec_and_meta(
            transaction_id=lot.transactionID,
            acq_date=lot.openDateTime.date(),
            currency=lot.currency,
            # the reducing side never asserts a number
            unlabeled_number_per=None,
            context=f"CLOSED_LOT {lot.symbol} open {lot.openDateTime}",
        )

    def _cost_spec_and_meta(
        self,
        transaction_id: str | None,
        acq_date: date,
        currency: str,
        unlabeled_number_per: Decimal | None,
        context: str,
    ) -> tuple[position.CostSpec, dict | None]:
        """Build the CostSpec and optional posting metadata for a lot posting.

        Post-threshold lots with a transactionID are labeled: the CostSpec
        carries ``label=transactionID`` and no number, so booking matches the
        exact lot.  Everything else keeps the unlabeled shape and, when a
        transactionID exists, records it as posting metadata for a later
        ledger migration.
        """
        post_threshold = self._labeled_since is not None and acq_date >= self._labeled_since
        labeled = post_threshold and transaction_id is not None
        if post_threshold and transaction_id is None:
            logger.warning(
                "IBKRImporter: no transactionID on {} -- falling back to unlabeled cost spec",
                context,
            )
        meta = None
        if not labeled and transaction_id is not None:
            meta = {"transactionID": str(transaction_id)}
        cost = position.CostSpec(
            number_per=None if labeled else unlabeled_number_per,
            number_total=None,
            currency=currency,
            date=acq_date,
            label=str(transaction_id) if labeled else None,
            merge=False,
        )
        return cost, meta

    def _buy_shares(self, trx, filepath: str, account_root: str) -> data.Transaction:
        symbol = self._map_symbol(trx.symbol)
        currency = trx.currency
        quantity = amount.Amount(trx.quantity, symbol)
        price = amount.Amount(trx.tradePrice, currency)
        proceeds = amount.Amount(round(trx.proceeds, 2), currency)
        commission = amount.Amount(round(trx.ibCommission, 2), trx.ibCommissionCurrency)

        cost, cost_meta = self._buy_cost_spec(trx)
        postings = [
            data.Posting(
                self._asset_account(symbol, account_root), quantity, cost, None, None, cost_meta
            ),
            data.Posting(
                self._liquidity_account(currency, account_root), proceeds, None, None, None, None
            ),
            data.Posting(
                self._liquidity_account(trx.ibCommissionCurrency, account_root),
                commission,
                None,
                None,
                None,
                None,
            ),
            data.Posting(
                self._fees_account_name(trx.ibCommissionCurrency, account_root),
                minus(commission),
                None,
                None,
                None,
                None,
            ),
        ]
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.dateTime.date(),
            "*",
            symbol,
            f"BUY {quantity.to_string()} @ {price.to_string()}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _sell_shares(self, trx, lots: list, filepath: str, account_root: str) -> data.Transaction:
        symbol = self._map_symbol(trx.symbol)
        currency = trx.currency
        proceeds = amount.Amount(round(trx.proceeds, 2), currency)
        price = amount.Amount(round(trx.tradePrice, 2), currency)
        quantity = amount.Amount(trx.quantity, symbol)
        commission = amount.Amount(round(trx.ibCommission, 2), trx.ibCommissionCurrency)

        lot_postings: list = []
        sum_qty = D("0")
        for lot in lots:
            sum_qty += lot.quantity
            if sum_qty > -trx.quantity:
                logger.warning(
                    "IBKRImporter: lot quantity over-match for sell {} on {}", symbol, trx.dateTime
                )
                break
            cost, cost_meta = self._sell_cost_spec(lot)
            lot_postings.append(
                data.Posting(
                    self._asset_account(symbol, account_root),
                    amount.Amount(-lot.quantity, symbol),
                    cost,
                    price,
                    None,
                    cost_meta,
                )
            )
            if sum_qty == -trx.quantity:
                break

        if sum_qty != -trx.quantity:
            logger.warning(
                "IBKRImporter: lot quantity mismatch for sell {} on {}: lots total {}, expected {}",
                symbol,
                trx.dateTime,
                sum_qty,
                -trx.quantity,
            )

        postings = (
            [
                data.Posting(
                    self._liquidity_account(currency, account_root),
                    proceeds,
                    None,
                    None,
                    None,
                    None,
                )
            ]
            + lot_postings
            + [
                data.Posting(self._pnl_account(symbol, account_root), None, None, None, None, None),
                data.Posting(
                    self._liquidity_account(trx.ibCommissionCurrency, account_root),
                    commission,
                    None,
                    None,
                    None,
                    None,
                ),
                data.Posting(
                    self._fees_account_name(trx.ibCommissionCurrency, account_root),
                    minus(commission),
                    None,
                    None,
                    None,
                    None,
                ),
            ]
        )
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.dateTime.date(),
            "*",
            symbol,
            f"SELL {quantity.to_string()} @ {price.to_string()}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    # ------------------------------------------------------------------
    # Cash transactions
    # ------------------------------------------------------------------

    def _cash_transactions(
        self, cash_txns, filepath: str, account_root: str, deposit_from: str | None
    ) -> list:
        """Accumulate cash transaction rows by type, then emit beancount entries."""
        div_buckets: dict[tuple, dict] = {}
        roc_rows: list = []
        deposit_rows: list = []
        interest_rows: list = []
        interest_wht_rows: list = []
        fee_rows: list = []

        simple_dispatch: dict = {
            CashAction.DEPOSITWITHDRAW: deposit_rows,
            CashAction.BROKERINTRCVD: interest_rows,
            CashAction.BROKERINTPAID: interest_rows,
            CashAction.FEES: fee_rows,
        }
        for trx in cash_txns:
            self._sort_cash_txn(trx, div_buckets, roc_rows, interest_wht_rows, simple_dispatch)

        return (
            self._emit_dividends(div_buckets, roc_rows, filepath, account_root)
            + self._emit_deposits(deposit_rows, filepath, account_root, deposit_from)
            + self._emit_safe(interest_rows, self._interest, "interest", filepath, account_root)
            + self._emit_safe(
                interest_wht_rows, self._interest_wht, "interest WHT", filepath, account_root
            )
            + self._emit_safe(fee_rows, self._fee, "fee", filepath, account_root)
        )

    def _sort_cash_txn(
        self,
        trx,
        div_buckets: dict,
        roc_rows: list,
        interest_wht_rows: list,
        simple_dispatch: dict,
    ) -> None:
        """Route one cash transaction row into the appropriate accumulator."""
        t = trx.type
        if t in (CashAction.DIVIDEND, CashAction.PAYMENTINLIEU):
            self._sort_dividend(trx, t, div_buckets, roc_rows)
        elif t == CashAction.WHTAX:
            self._sort_wht(trx, div_buckets, interest_wht_rows)
        elif t in simple_dispatch:
            simple_dispatch[t].append(trx)
        else:
            logger.warning("IBKRImporter: unrecognised CashAction type: {!r}", t)

    def _sort_dividend(self, trx, t: CashAction, div_buckets: dict, roc_rows: list) -> None:
        """Accumulate a DIVIDEND or PAYMENTINLIEU row."""
        if _ROC_STR in (trx.description or ""):
            roc_rows.append(trx)
        else:
            key = (trx.symbol, trx.reportDate, t)
            div_buckets.setdefault(key, {"div": None, "wht": None})["div"] = trx

    def _sort_wht(self, trx, div_buckets: dict, interest_wht_rows: list) -> None:
        """Match a WHTAX row to its dividend bucket, or route to interest WHT list."""
        wht_type = _wht_div_type(trx.description or "")
        if wht_type == "interest":
            interest_wht_rows.append(trx)
        elif wht_type is not None:
            key = (trx.symbol, trx.reportDate, wht_type)
            div_buckets.setdefault(key, {"div": None, "wht": None})["wht"] = trx
        else:
            logger.warning(
                "IBKRImporter: unrecognised WHT description: {!r}", trx.description or ""
            )

    def _emit_dividends(
        self, div_buckets: dict, roc_rows: list, filepath: str, account_root: str
    ) -> list:
        """Emit Transaction entries for all accumulated dividends and ROC rows."""
        entries: list = []
        for key, bucket in div_buckets.items():
            if bucket["div"] is None:
                logger.warning("IBKRImporter: WHT row has no matching dividend: {}", key)
                continue
            try:
                entries.append(self._dividend(bucket["div"], bucket["wht"], filepath, account_root))
            except Exception as exc:
                logger.warning("IBKRImporter: skipping dividend {}: {}", key, exc)
        for trx in roc_rows:
            try:
                entries.append(self._dividend(trx, None, filepath, account_root))
            except Exception as exc:
                logger.warning("IBKRImporter: skipping Return of Capital {}: {}", trx.symbol, exc)
        return entries

    def _emit_deposits(
        self, rows: list, filepath: str, account_root: str, deposit_from: str | None
    ) -> list:
        """Emit deposit/withdrawal entries."""
        return [self._deposit(trx, filepath, account_root, deposit_from) for trx in rows]

    def _emit_safe(self, rows: list, method, label: str, filepath: str, account_root: str) -> list:
        """Call *method(trx, filepath, account_root)* for each row; warn and continue on error."""
        entries: list = []
        for trx in rows:
            try:
                entries.append(method(trx, filepath, account_root))
            except Exception as exc:
                logger.warning("IBKRImporter: skipping {} entry: {}", label, exc)
        return entries

    # ------------------------------------------------------------------
    # Transaction builders
    # ------------------------------------------------------------------

    def _dividend(self, trx, wht_trx, filepath: str, account_root: str) -> data.Transaction:
        symbol = self._map_symbol(trx.symbol)
        currency = trx.currency
        div_amt = amount.Amount(round(trx.amount, 2), currency)

        desc = trx.description or ""

        # ISIN from description parentheses, e.g. "(US9229083632)"
        isin = _ROC_STR if _ROC_STR in desc else ""
        if not isin:
            isin_match = _RE_ISIN.search(desc)
            isin = isin_match.group(1) if isin_match else ""

        # Per-share amount, e.g. "0.8700 USD PER SHARE"
        pershare_match = _RE_PER_SHARE.search(desc)
        pershare = pershare_match.group("amount") if pershare_match else ""

        in_lieu = bool(_RE_WHT_PIL.match(desc))
        narration = f"Dividend {symbol}" + (" in lieu" if in_lieu else "")

        postings = [
            data.Posting(
                self._div_income_account(currency, symbol, account_root),
                minus(div_amt),
                None,
                None,
                None,
                None,
            ),
        ]

        if wht_trx is not None:
            if wht_trx.currency != currency:
                logger.warning(
                    "IBKRImporter: dividend/WHT currency mismatch for {}: {} vs {} -- skipping WHT",
                    symbol,
                    currency,
                    wht_trx.currency,
                )
            else:
                wht_amt = amount.Amount(round(wht_trx.amount, 2), wht_trx.currency)
                postings.extend(
                    [
                        data.Posting(
                            self._wht_account_name(symbol, account_root),
                            minus(wht_amt),
                            None,
                            None,
                            None,
                            None,
                        ),
                        data.Posting(
                            self._liquidity_account(currency, account_root),
                            amount_add(div_amt, wht_amt),
                            None,
                            None,
                            None,
                            None,
                        ),
                    ]
                )
        else:
            postings.append(
                data.Posting(
                    self._liquidity_account(currency, account_root), div_amt, None, None, None, None
                )
            )

        meta = data.new_metadata(filepath, 0, {"isin": isin, "per_share": pershare})
        return data.Transaction(
            meta,
            trx.reportDate,
            "*",
            symbol,
            narration,
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _interest(self, trx, filepath: str, account_root: str) -> data.Transaction:
        currency = trx.currency
        amt = amount.Amount(round(trx.amount, 2), currency)
        postings = [
            data.Posting(
                self._interest_account(currency, account_root), minus(amt), None, None, None, None
            ),
            data.Posting(
                self._liquidity_account(currency, account_root), amt, None, None, None, None
            ),
        ]
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.reportDate,
            "*",
            "IB",
            trx.description or f"Interest {currency}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _interest_wht(self, trx, filepath: str, account_root: str) -> data.Transaction:
        currency = trx.currency
        amt = amount.Amount(round(trx.amount, 2), currency)
        postings = [
            data.Posting(
                self._wht_account_name(currency, account_root), minus(amt), None, None, None, None
            ),
            data.Posting(
                self._liquidity_account(currency, account_root), amt, None, None, None, None
            ),
        ]
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.reportDate,
            "*",
            "IB",
            trx.description or f"Withholding tax on interest {currency}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _fee(self, trx, filepath: str, account_root: str) -> data.Transaction:
        currency = trx.currency
        amt = amount.Amount(round(trx.amount, 2), currency)
        month_match = _RE_FEE_MONTH.search(trx.description or "")
        month = month_match.group(0) if month_match else trx.description or ""
        postings = [
            data.Posting(
                self._fees_account_name(currency, account_root), minus(amt), None, None, None, None
            ),
            data.Posting(
                self._liquidity_account(currency, account_root), amt, None, None, None, None
            ),
        ]
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.reportDate,
            "*",
            "IB",
            f"Fee {currency} {month}".strip(),
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _deposit(
        self, trx, filepath: str, account_root: str, deposit_from: str | None
    ) -> data.Transaction:
        currency = trx.currency
        amt = amount.Amount(round(trx.amount, 2), currency)
        ibkr_leg = data.Posting(
            self._liquidity_account(currency, account_root), amt, None, None, None, None
        )
        if deposit_from:
            flag = "*"
            postings = [
                data.Posting(deposit_from, minus(amt), None, None, None, None),
                ibkr_leg,
            ]
        else:
            flag = "!"
            postings = [ibkr_leg]
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.reportDate,
            flag,
            "self",
            "deposit / withdrawal",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------

    def _transfers(
        self, transfers, filepath: str, account_root: str
    ) -> list:
        """Accumulate transfer rows by type, then emit beancount entries."""
        cash_xfers: list = []
        sec_xfers: list = []

        for trx in transfers:
            self._sort_xfer_txn(trx, cash_xfers, sec_xfers)

        return ([self._cash_xfer(trx, filepath, account_root) for trx in cash_xfers] +
                [self._sec_xfer(trx, filepath, account_root) for trx in sec_xfers])

    def _sort_xfer_txn(
        self,
        trx,
        cash_xfers: list,
        sec_xfers: list,
    ) -> None:
        """Route transfer into the appropriate accumulator."""
        if trx.type and trx.symbol:
            sec_xfers.append(trx)
        elif trx.type and trx.cashTransfer:
            cash_xfers.append(trx)

    def _cash_xfer(
        self, trx, filepath: str, account_root: str
    ) -> data.Transaction:
        currency = trx.currency
        amt = amount.Amount(round(trx.cashTransfer, 2), currency)
        local_leg = data.Posting(
            self._liquidity_account(currency, account_root), amt, None, None, None, None
        )
        flag = "!"
        postings = [local_leg]
        description = "{} {} {}".format(currency, trx.type.value,
                                        trx.description if trx.description else "TRANSFER")
        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.reportDate,
            flag,
            trx.account,
            description,
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _sec_xfer(
        self, trx, filepath: str, account_root: str
    ) -> data.Transaction:
        symbol = trx.symbol
        quantity = amount.Amount(trx.quantity, symbol)
        local_leg = data.Posting(
            self._asset_account(symbol, account_root), quantity, None, None, None, None
        )
        flag = "!"
        postings = [local_leg]

        return data.Transaction(
            data.new_metadata(filepath, 0),
            trx.reportDate,
            flag,
            trx.account,
            f"{trx.type.value} TRANSFER {trx.description}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    # ------------------------------------------------------------------
    # Balances
    # ------------------------------------------------------------------

    def _balances(self, cash_report, account_root: str) -> list:
        """Emit one Balance directive per non-summary CashReport row."""
        entries: list = []
        for cr in cash_report:
            if str(cr.currency) == "BASE_SUMMARY":
                continue
            try:
                currency = cr.currency
                bal_amt = amount.Amount(round(cr.endingCash, 2), currency)
                entries.append(
                    data.Balance(
                        data.new_metadata("balance", 0),
                        cr.toDate + timedelta(days=1),
                        self._liquidity_account(currency, account_root),
                        bal_amt,
                        None,
                        None,
                    )
                )
            except Exception as exc:
                logger.warning("IBKRImporter: skipping balance entry: {}", exc)
        return entries

    # ------------------------------------------------------------------
    # Account name helpers
    # ------------------------------------------------------------------

    def _liquidity_account(self, currency: str, account_root: str) -> str:
        return f"{account_root}:{currency}"

    def _asset_account(self, symbol: str, account_root: str) -> str:
        if self._asset_suffix:
            return f"{account_root}:{self._asset_suffix}"
        return f"{account_root}:{symbol}"

    def _div_income_account(self, currency: str, symbol: str, account_root: str) -> str:
        if self._div_account:
            return self._div_account
        return "{}:{}".format(self._asset_account(symbol, account_root).replace('Assets', 'Income'),
                              self._div_suffix)

    def _interest_account(self, currency: str, account_root: str) -> str:
        return f"{account_root.replace('Assets', 'Income')}:{self._interest_suffix}:{currency}"

    def _wht_account_name(self, identifier: str, account_root: str) -> str:
        """WHT account for a symbol (dividends) or currency (interest WHT)."""
        if self._wht_account:
            return f"{self._wht_account}:{identifier}"
        return f"{account_root.replace('Assets', 'Expenses')}:WHT:{identifier}"

    def _fees_account_name(self, currency: str, account_root: str) -> str:
        if self._fees_account:
            return self._fees_account
        return f"{account_root.replace('Assets', 'Expenses')}:{self._fees_suffix}:{currency}"

    def _pnl_account(self, symbol: str, account_root: str) -> str:
        return "{}:{}".format(self._asset_account(symbol, account_root).replace('Assets', 'Income'),
                              self._pnl_suffix)

    # ------------------------------------------------------------------
    # Symbol normalisation
    # ------------------------------------------------------------------

    def _map_symbol(self, symbol: str) -> str:
        """Normalise an IBKR symbol, then apply user-configured remapping.

        Auto-cleanup (applied in order):
        1. Strip trailing ``z`` that IBKR appends to certain bond symbols.
        2. Strip exchange suffix -- everything from the last ``.`` onward
           (e.g. ``VWRL.BATS`` -> ``VWRL``).

        Explicit entries in *symbol_map* take priority over auto-cleanup.
        """
        s = symbol.rstrip("z")
        if "." in s:
            s = s.rsplit(".", 1)[0]
        return self._symbol_map.get(s, s)
