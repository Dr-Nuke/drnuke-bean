"""
IBKR FlexQuery download helper.

This module is deliberately separate from the importer.  It owns the
network-side concern: fetching XML from the IBKR Flex Web Service and
caching it on disk.  The importer (ibkr.py) is a pure file parser and has
no knowledge of tokens, query IDs, or HTTP.

Caching
-------
The raw FlexQuery XML response is cached in a ``diskcache.Cache`` stored
under ``{dest_dir}/.cache``, keyed by ``(query_name, date_from, date_to)``.
The TTL expires at midnight of the current day.  On a cache hit the response
is read from disk cache; the IBKR API is not contacted.  The cache persists
across process runs — no in-memory state is involved.

On every call (hit or miss) the response is written to
``{dest_dir}/ibkr_{actual_from}_{actual_to}.xml``, where the dates are
extracted from the ``FlexStatement`` element in the XML response.
The file write is skipped if the file already exists.

Usage in run_imports.py::

    from drnukebean.importer.ibkr_flexquery import make_ibkr_setup

    pipelines = [
        dict(
            name='ibkr',
            importer=IBKRImporter(...),
            source_dir=_ibkr_dir,
            setup=make_ibkr_setup(
                token=cfg.IBKR_TOKEN,
                query_id=cfg.IBKR_QUERY_ID,
                query_name=cfg.IBKR_QUERY_NAME,
                dest_dir=_ibkr_dir,
            ),
            ...
        ),
    ]

    The runner calls setup(date_from, date_to) with the resolved date range
    (first day of each month) derived from --from / --to CLI flags or the
    run_all() programmatic params.
"""

from __future__ import annotations

import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import diskcache
from ibflex import client
from ibflex.client import IbflexClientError, ResponseCodeError, StatementGenerationTimeout
from loguru import logger

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_FLEX_ROOT_TAG = "FlexQueryResponse"

# GetStatement endpoint as documented for Flex Web Service v3.  Deliberately
# NOT taken from the SendRequest response: IBKR still returns a <Url> on the
# legacy host gdcdyn.interactivebrokers.com, which no longer resolves
# (Nov 2026), and ibflex >= 1.2 prefers that returned Url over its own
# STMT_URL.  Also not taken from ibflex.client.STMT_URL, which pointed to
# gdcdyn in older ibflex releases.
_STMT_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"

# Poll limits for the GetStatement step.
_MAX_TRIES = 5
# Error codes that IBKR documents as "please try again shortly", in addition to
# those ibflex.client.check_statement_response already treats as retryable
# (1009, 1018, 1019).
_EXTRA_RETRY_CODES = frozenset({"1001", "1004", "1021"})
_EXTRA_RETRY_DELAY = 5


def _download(token: str, query_id: str, max_tries: int = _MAX_TRIES) -> bytes:
    """2-step FlexQuery download, always polling GetStatement on ``_STMT_URL``.

    Replaces ``ibflex.client.download``, which follows the (stale) Url
    returned by SendRequest.  Uses ibflex's request_statement for step 1 and
    its check_statement_response for retry/error classification.

    Raises:
        ResponseCodeError:          non-retryable IBKR error code.
        StatementGenerationTimeout: still not ready after *max_tries* polls.
        BadResponseError:           unparseable IBKR response.
    """
    stmt_access = client.request_statement(token, query_id)
    if stmt_access.Url and stmt_access.Url != _STMT_URL:
        logger.debug(
            "IBKR FlexQuery: ignoring returned GetStatement Url {} in favour of {}",
            stmt_access.Url,
            _STMT_URL,
        )

    for attempt in range(1, max_tries + 1):
        response = client.submit_request(
            url=_STMT_URL, token=token, query=stmt_access.ReferenceCode
        )
        try:
            status = client.check_statement_response(response)
        except ResponseCodeError as exc:
            if exc.code not in _EXTRA_RETRY_CODES:
                raise
            status = _EXTRA_RETRY_DELAY
        if status is True:
            return response.content
        if attempt < max_tries:
            logger.info(
                "IBKR FlexQuery: statement not ready (attempt {}/{}), retrying in {}s",
                attempt,
                max_tries,
                status,
            )
            time.sleep(status)

    raise StatementGenerationTimeout(
        f"IBKR FlexQuery: statement not ready after {max_tries} attempts"
    )


def _extract_statement_dates(raw: str | bytes) -> tuple[str | None, str | None]:
    """Parse fromDate/toDate from the first FlexStatement element in the XML.

    IBKR encodes dates as YYYYMMDD; returns ISO strings (YYYY-MM-DD) or None.
    """
    try:
        # Own IBKR FlexQuery export, not untrusted input.
        root = ET.fromstring(  # noqa: S314
            raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        )
        for stmt in root.iter("FlexStatement"):
            from_raw = stmt.get("fromDate")
            to_raw = stmt.get("toDate")
            if from_raw and len(from_raw) == 8:
                from_raw = f"{from_raw[:4]}-{from_raw[4:6]}-{from_raw[6:]}"
            if to_raw and len(to_raw) == 8:
                to_raw = f"{to_raw[:4]}-{to_raw[4:6]}-{to_raw[6:]}"
            return from_raw, to_raw
    except ET.ParseError:
        pass
    return None, None


def _seconds_until_midnight() -> float:
    """Seconds remaining until the next calendar-day boundary."""
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


def _extract_query_name(raw: str | bytes) -> str | None:
    """Return the ``queryName`` attribute from the XML root element, or None."""
    try:
        # Own IBKR FlexQuery export, not untrusted input.
        root = ET.fromstring(  # noqa: S314
            raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        )
        return root.get("queryName")
    except ET.ParseError:
        return None


# ---------------------------------------------------------------------------
# Internal: cache-backed download
# ---------------------------------------------------------------------------


def _get_response(
    cache: diskcache.Cache,
    cache_key: tuple,
    token: str,
    query_id: str,
    query_name: str,
) -> str | bytes:
    """Return cached response or download from IBKR, validating queryName."""
    if cache_key in cache:
        logger.info("IBKR FlexQuery: cache hit ({})", query_name)
        return cast("str | bytes", cache[cache_key])

    logger.info("IBKR FlexQuery: downloading ({}) via API", query_name)
    try:
        response = _download(str(token), str(query_id))
    except IbflexClientError as exc:
        logger.error("IBKR FlexQuery download failed: {}", exc)
        raise RuntimeError(f"IBKR FlexQuery download failed: {exc}") from exc

    actual = _extract_query_name(response)
    if actual != query_name:
        raise RuntimeError(
            f"IBKR FlexQuery response queryName mismatch: "
            f"expected {query_name!r}, got {actual!r}. "
            f"Check token and query_id in local_config.py."
        )

    cache.set(cache_key, response, expire=_seconds_until_midnight())
    return response


def _write_xml(response: str | bytes, xml_file: Path) -> None:
    """Write *response* to *xml_file* unless the file already exists."""
    if xml_file.exists():
        return
    if isinstance(response, bytes):
        xml_file.write_bytes(response)
    else:
        xml_file.write_text(response, encoding="utf-8")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_ibkr_setup(
    token: str,
    query_id: str,
    query_name: str,
    dest_dir: str | Path,
) -> Callable[[date, date], None]:
    """Return a setup callable for use in the pipeline.

    The callable is invoked by the runner as ``setup(date_from, date_to)``
    where both dates are the first day of their respective months.

    On each invocation the callable checks a ``diskcache.Cache`` for a cached
    copy of the FlexQuery response.  On a cache hit the IBKR API is not
    contacted.  On a cache miss the response is fetched, validated, and stored
    in the cache with a TTL that expires at midnight.

    In both cases the response XML is written to
    ``{dest_dir}/ibkr_{actual_from}_{actual_to}.xml``, where the dates are
    parsed from the ``FlexStatement`` element in the response (skipped if
    the file already exists).

    Args:
        token:       IBKR Flex Web Service token (from local_config.py).
        query_id:    IBKR Flex Query ID (from local_config.py).
        query_name:  Expected ``queryName`` in the FlexQueryResponse root.
                     Validated on download; also part of the cache key.
        dest_dir:    Directory where the XML file is saved.  Should match the
                     ``source_dir`` of the corresponding pipeline entry.
    """

    def setup(date_from: date, date_to: date) -> None:
        if "--from" in sys.argv or "--to" in sys.argv:
            logger.warning(
                "IBKR FlexQuery: requested date range ({} -> {}) is ignored; "
                "the report period is determined by the portal configuration.",
                date_from,
                date_to,
            )

        dest = Path(dest_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)

        cache_key = (query_name, date_from.isoformat(), date_to.isoformat())
        with diskcache.Cache(dest / ".cache") as cache:
            response = _get_response(cache, cache_key, token, query_id, query_name)

        actual_from, actual_to = _extract_statement_dates(response)
        if actual_from and actual_to:
            xml_name = f"ibkr_{actual_from}_{actual_to}.xml"
        else:
            logger.warning(
                "IBKR FlexQuery: could not parse statement dates from XML; "
                "falling back to requested dates"
            )
            xml_name = f"ibkr_{date_from.isoformat()}_{date_to.isoformat()}.xml"

        _write_xml(response, dest / xml_name)

    return setup
