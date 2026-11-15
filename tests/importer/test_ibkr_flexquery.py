"""Unit tests for drnukebean.importer.ibkr_flexquery (network/cache layer).

ibkr_flexquery._download is mocked at the module boundary (its own tests mock
the ibflex.client functions it calls) -- no real HTTP requests are made.
diskcache.Cache is backed by a temporary directory so tests are fully isolated.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import drnukebean.importer.ibkr_flexquery as fq_module
from drnukebean.importer.ibkr_flexquery import (
    _extract_query_name,
    _extract_statement_dates,
    _seconds_until_midnight,
    make_ibkr_setup,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ibkr"

# Minimal valid XML that satisfies _extract_query_name and _extract_statement_dates
_SAMPLE_XML = (FIXTURES / "sample_flexquery_response.xml").read_bytes()

_QUERY_NAME = "TestQuery"
_TOKEN = "EXAMPLE_TOKEN_12345"
_QUERY_ID = "999999"

_WRONG_QUERY_XML = (
    b'<FlexQueryResponse queryName="WrongQuery" type="AF">'
    b'<FlexStatements count="0"/></FlexQueryResponse>'
)

DATE_FROM = datetime.date(2024, 1, 1)
DATE_TO = datetime.date(2024, 1, 31)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup(dest_dir: Path, mocker, response=_SAMPLE_XML, query_name=_QUERY_NAME):
    """Return a configured setup callable with a mocked download."""
    mocker.patch.object(fq_module, "_download", return_value=response)
    return make_ibkr_setup(
        token=_TOKEN,
        query_id=_QUERY_ID,
        query_name=query_name,
        dest_dir=dest_dir,
    )


# ===========================================================================
# Pure helper functions
# ===========================================================================


class TestExtractStatementDates:
    def test_parses_yyyymmdd_format(self):
        xml = (
            b'<FlexQueryResponse><FlexStatement fromDate="20240101" '
            b'toDate="20240131"/></FlexQueryResponse>'
        )
        from_d, to_d = _extract_statement_dates(xml)
        assert from_d == "2024-01-01"
        assert to_d == "2024-01-31"

    def test_accepts_str_input(self):
        xml = (
            '<FlexQueryResponse><FlexStatement fromDate="20240201" '
            'toDate="20240229"/></FlexQueryResponse>'
        )
        from_d, to_d = _extract_statement_dates(xml)
        assert from_d == "2024-02-01"
        assert to_d == "2024-02-29"

    def test_returns_none_on_malformed_xml(self):
        from_d, to_d = _extract_statement_dates(b"not xml at all <<<")
        assert from_d is None
        assert to_d is None

    def test_returns_none_when_no_statement_element(self):
        xml = b"<FlexQueryResponse/>"
        from_d, to_d = _extract_statement_dates(xml)
        assert from_d is None
        assert to_d is None


class TestExtractQueryName:
    def test_extracts_query_name(self):
        xml = b'<FlexQueryResponse queryName="MyQuery" type="AF"/>'
        assert _extract_query_name(xml) == "MyQuery"

    def test_returns_none_when_absent(self):
        xml = b"<FlexQueryResponse/>"
        assert _extract_query_name(xml) is None

    def test_returns_none_on_malformed_xml(self):
        assert _extract_query_name(b"<<<") is None


class TestSecondsUntilMidnight:
    def test_returns_positive_float(self):
        secs = _seconds_until_midnight()
        assert isinstance(secs, float)
        assert 0 < secs <= 86400


# ===========================================================================
# make_ibkr_setup — cache miss path
# ===========================================================================


class TestCacheMiss:
    def test_download_called_on_cache_miss(self, tmp_path, mocker):
        mock_dl = mocker.patch.object(fq_module, "_download", return_value=_SAMPLE_XML)
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        setup(DATE_FROM, DATE_TO)
        mock_dl.assert_called_once_with(_TOKEN, _QUERY_ID)

    def test_xml_file_written_on_cache_miss(self, tmp_path, mocker):
        setup = _setup(tmp_path, mocker)
        setup(DATE_FROM, DATE_TO)
        xml_files = list(tmp_path.glob("ibkr_*.xml"))
        assert len(xml_files) == 1

    def test_xml_filename_uses_statement_dates(self, tmp_path, mocker):
        setup = _setup(tmp_path, mocker)
        setup(DATE_FROM, DATE_TO)
        xml_files = list(tmp_path.glob("ibkr_*.xml"))
        assert xml_files[0].name == "ibkr_2024-01-01_2024-01-31.xml"

    def test_xml_file_content_matches_response(self, tmp_path, mocker):
        setup = _setup(tmp_path, mocker)
        setup(DATE_FROM, DATE_TO)
        xml_file = next(tmp_path.glob("ibkr_*.xml"))
        assert xml_file.read_bytes() == _SAMPLE_XML

    def test_response_stored_in_cache(self, tmp_path, mocker):
        import diskcache

        setup = _setup(tmp_path, mocker)
        setup(DATE_FROM, DATE_TO)
        cache_key = (_QUERY_NAME, DATE_FROM.isoformat(), DATE_TO.isoformat())
        with diskcache.Cache(tmp_path / ".cache") as cache:
            assert cache_key in cache
            assert cache[cache_key] == _SAMPLE_XML


# ===========================================================================
# make_ibkr_setup — cache hit path
# ===========================================================================


class TestCacheHit:
    def _prime_cache(self, tmp_path):
        """Populate cache directly without network."""
        import diskcache

        cache_key = (_QUERY_NAME, DATE_FROM.isoformat(), DATE_TO.isoformat())
        with diskcache.Cache(tmp_path / ".cache") as cache:
            cache.set(cache_key, _SAMPLE_XML, expire=3600)

    def test_download_not_called_on_cache_hit(self, tmp_path, mocker):
        self._prime_cache(tmp_path)
        mock_dl = mocker.patch.object(fq_module, "_download", return_value=_SAMPLE_XML)
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        setup(DATE_FROM, DATE_TO)
        mock_dl.assert_not_called()

    def test_xml_file_written_from_cache(self, tmp_path, mocker):
        self._prime_cache(tmp_path)
        mocker.patch.object(fq_module, "_download", return_value=_SAMPLE_XML)
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        setup(DATE_FROM, DATE_TO)
        xml_files = list(tmp_path.glob("ibkr_*.xml"))
        assert len(xml_files) == 1

    def test_existing_xml_not_overwritten(self, tmp_path, mocker):
        self._prime_cache(tmp_path)
        mocker.patch.object(fq_module, "_download", return_value=_SAMPLE_XML)
        # Pre-create the file with known content
        xml_file = tmp_path / "ibkr_2024-01-01_2024-01-31.xml"
        xml_file.write_bytes(b"original content")
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        setup(DATE_FROM, DATE_TO)
        assert xml_file.read_bytes() == b"original content"


# ===========================================================================
# queryName mismatch
# ===========================================================================


class TestQueryNameMismatch:
    def test_raises_on_queryname_mismatch(self, tmp_path, mocker):
        mocker.patch.object(fq_module, "_download", return_value=_WRONG_QUERY_XML)
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        with pytest.raises(RuntimeError, match="queryName mismatch"):
            setup(DATE_FROM, DATE_TO)

    def test_cache_not_populated_on_mismatch(self, tmp_path, mocker):
        import diskcache

        mocker.patch.object(fq_module, "_download", return_value=_WRONG_QUERY_XML)
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        try:
            setup(DATE_FROM, DATE_TO)
        except RuntimeError:
            pass
        cache_key = (_QUERY_NAME, DATE_FROM.isoformat(), DATE_TO.isoformat())
        with diskcache.Cache(tmp_path / ".cache") as cache:
            assert cache_key not in cache

    def test_no_xml_file_written_on_mismatch(self, tmp_path, mocker):
        mocker.patch.object(fq_module, "_download", return_value=_WRONG_QUERY_XML)
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        try:
            setup(DATE_FROM, DATE_TO)
        except RuntimeError:
            pass
        assert list(tmp_path.glob("ibkr_*.xml")) == []


# ===========================================================================
# ResponseCodeError from download
# ===========================================================================


class TestDownloadError:
    def test_response_code_error_raises_runtime_error(self, tmp_path, mocker):
        from ibflex.client import ResponseCodeError

        mocker.patch.object(
            fq_module, "_download", side_effect=ResponseCodeError("1012", "Account not found")
        )
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        with pytest.raises(RuntimeError, match="IBKR FlexQuery download failed"):
            setup(DATE_FROM, DATE_TO)


# ===========================================================================
# Fallback filename when statement dates unparseable
# ===========================================================================


class TestFallbackFilename:
    def test_uses_requested_dates_when_statement_dates_missing(self, tmp_path, mocker):
        # XML without FlexStatement element -> dates are None -> fallback to requested dates
        bare_xml = (
            b'<FlexQueryResponse queryName="TestQuery" type="AF">'
            b'<FlexStatements count="0"/></FlexQueryResponse>'
        )
        mocker.patch.object(fq_module, "_download", return_value=bare_xml)
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        setup(DATE_FROM, DATE_TO)
        xml_files = list(tmp_path.glob("ibkr_*.xml"))
        assert len(xml_files) == 1
        assert xml_files[0].name == f"ibkr_{DATE_FROM}_{DATE_TO}.xml"

    def test_statement_generation_timeout_raises_runtime_error(self, tmp_path, mocker):
        from ibflex.client import StatementGenerationTimeout

        mocker.patch.object(fq_module, "_download", side_effect=StatementGenerationTimeout("x"))
        setup = make_ibkr_setup(_TOKEN, _QUERY_ID, _QUERY_NAME, tmp_path)
        with pytest.raises(RuntimeError, match="IBKR FlexQuery download failed"):
            setup(DATE_FROM, DATE_TO)


# ===========================================================================
# _download — 2-step protocol against mocked ibflex.client primitives
# ===========================================================================

_LEGACY_URL = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
_REF_CODE = "1234567890"


def _stmt_error(code: str, msg: str = "Please try again shortly.") -> SimpleNamespace:
    """Fake requests.Response carrying a FlexStatementResponse error."""
    return SimpleNamespace(
        content=(
            f'<FlexStatementResponse timestamp="15 November, 2026 05:44 AM EST">'
            f"<Status>Fail</Status><ErrorCode>{code}</ErrorCode>"
            f"<ErrorMessage>{msg}</ErrorMessage></FlexStatementResponse>"
        ).encode()
    )


class TestDownload:
    @pytest.fixture
    def flex(self, mocker):
        """Mock request_statement (returning the legacy Url) and submit_request."""
        from ibflex.client import StatementAccess

        mocker.patch.object(
            fq_module.client,
            "request_statement",
            return_value=StatementAccess(
                timestamp=datetime.datetime(2026, 11, 15),
                ReferenceCode=_REF_CODE,
                Url=_LEGACY_URL,
            ),
        )
        submit = mocker.patch.object(fq_module.client, "submit_request")
        sleep = mocker.patch.object(fq_module.time, "sleep")
        return SimpleNamespace(submit=submit, sleep=sleep)

    def test_returns_content_on_first_success(self, flex):
        flex.submit.return_value = SimpleNamespace(content=_SAMPLE_XML)
        assert fq_module._download(_TOKEN, _QUERY_ID) == _SAMPLE_XML
        flex.sleep.assert_not_called()

    def test_ignores_returned_legacy_url(self, flex):
        flex.submit.return_value = SimpleNamespace(content=_SAMPLE_XML)
        fq_module._download(_TOKEN, _QUERY_ID)
        flex.submit.assert_called_once_with(url=fq_module._STMT_URL, token=_TOKEN, query=_REF_CODE)
        assert "ndcdyn.interactivebrokers.com" in fq_module._STMT_URL

    @pytest.mark.parametrize("code", ["1019", "1009", "1018", "1001", "1004", "1021"])
    def test_retries_on_transient_codes(self, flex, code):
        flex.submit.side_effect = [_stmt_error(code), SimpleNamespace(content=_SAMPLE_XML)]
        assert fq_module._download(_TOKEN, _QUERY_ID) == _SAMPLE_XML
        assert flex.submit.call_count == 2
        flex.sleep.assert_called_once()

    def test_raises_on_non_retryable_code(self, flex):
        from ibflex.client import ResponseCodeError

        flex.submit.return_value = _stmt_error("1015", "Token is invalid.")
        with pytest.raises(ResponseCodeError, match="1015"):
            fq_module._download(_TOKEN, _QUERY_ID)
        assert flex.submit.call_count == 1

    def test_timeout_after_max_tries(self, flex):
        from ibflex.client import StatementGenerationTimeout

        flex.submit.return_value = _stmt_error("1019")
        with pytest.raises(StatementGenerationTimeout):
            fq_module._download(_TOKEN, _QUERY_ID, max_tries=3)
        assert flex.submit.call_count == 3
        assert flex.sleep.call_count == 2
