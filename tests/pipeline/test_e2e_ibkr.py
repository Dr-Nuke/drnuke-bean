"""End-to-end tests: mocked IBKR download -> pipeline -> beancount output.

Chain exercised (nothing mocked except the network layer):
    drnukebean.importer.ibkr_flexquery._download  (mocked, returns real obfuscated fixture XML)
    -> make_ibkr_setup      (diskcache + write XML to source_dir)
    -> run_all              (identify -> extract -> _append_entries)
    -> parse_string         (beancount syntax validation)

The diskcache, IBKRImporter, runner, and printer all run against real code.
Semantic beancount validity (account declarations, balance assertions) is
out of scope here — see QA.md "Optional, for later".
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest
from beancount.parser import parser as bc_parser

import drnukebean.importer.ibkr_flexquery as fq_module
from drnukebean.importer.ibkr import IBKRImporter
from drnukebean.importer.ibkr_flexquery import make_ibkr_setup
from drnukebean.pipeline.runner import run_all

FIXTURE = Path(__file__).parent.parent / "fixtures" / "ibkr" / "flexquery.xml"
_QUERY_NAME = "beancount flexquery"
_ACCOUNT = "Assets:Invest:IBKR"
_TOKEN = "EXAMPLE_TOKEN_12345"
_QUERY_ID = "999999"

DATE_FROM = datetime.date(2024, 1, 1)
DATE_TO = datetime.date(2024, 1, 31)

# Expected XML filename written by make_ibkr_setup (dates parsed from fixture)
_EXPECTED_XML = "ibkr_2024-01-01_2024-01-31.xml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(source_dir: Path, bean_output_file: Path) -> list[dict]:
    """Build a pipeline entry wiring real IBKRImporter to the real flexquery fixture."""
    setup = make_ibkr_setup(
        token=_TOKEN,
        query_id=_QUERY_ID,
        query_name=_QUERY_NAME,
        dest_dir=source_dir,
    )
    importer = IBKRImporter(
        account=_ACCOUNT,
        query_name=_QUERY_NAME,
        currency="EUR",
    )
    return [
        dict(
            name="ibkr",
            importer=importer,
            source_dir=source_dir,
            bean_output_file=bean_output_file,
            setup=setup,
        )
    ]


def _run(
    pipeline: list[dict],
    *,
    dry_run: bool = False,
    dry_run_file: str | None = None,
) -> None:
    run_all(
        pipeline,
        dry_run=dry_run,
        dry_run_file=dry_run_file,
        date_from=DATE_FROM,
        date_to=DATE_TO,
    )


# ===========================================================================
# Full run
# ===========================================================================


class TestFullRun:
    @pytest.fixture(autouse=True)
    def _mock_download(self, mocker):
        mocker.patch.object(fq_module, "_download", return_value=FIXTURE.read_bytes())

    @pytest.fixture(autouse=True)
    def _clean_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

    def test_output_file_created(self, tmp_path):
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"

        _run(_make_pipeline(src, out))

        assert out.exists()
        assert out.stat().st_size > 0

    def test_output_is_valid_beancount_syntax(self, tmp_path):
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"

        _run(_make_pipeline(src, out))

        _, errors, _ = bc_parser.parse_string(out.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)

    def test_output_entry_count(self, tmp_path):
        """Fixture has ~24 entries (4 balances + 6 deposits + 5 sells + 4 buys
        + 1 interest + 3 forex + 2 dividends); assert well above zero."""
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"

        _run(_make_pipeline(src, out))

        entries, _, _ = bc_parser.parse_string(out.read_text())
        assert len(entries) > 20

    def test_setup_writes_xml_to_source_dir(self, tmp_path):
        """make_ibkr_setup must write the XML file that identify() subsequently finds."""
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"

        _run(_make_pipeline(src, out))

        xml_files = [f for f in src.iterdir() if f.suffix == ".xml"]
        assert len(xml_files) == 1
        assert xml_files[0].name == _EXPECTED_XML


# ===========================================================================
# Labeled run (transactionID_labeled_since set)
# ===========================================================================


class TestLabeledRun:
    """Threshold 2024-01-20 splits the fixture into labeled (BUYs on
    2024-01-22 / 2024-01-29) and unlabeled (earlier BUYs, all closed lots
    opened in 2023) postings."""

    @pytest.fixture(autouse=True)
    def _mock_download(self, mocker):
        mocker.patch.object(fq_module, "_download", return_value=FIXTURE.read_bytes())

    @pytest.fixture(autouse=True)
    def _clean_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

    def _make_labeled_pipeline(self, source_dir: Path, bean_output_file: Path) -> list[dict]:
        setup = make_ibkr_setup(
            token=_TOKEN,
            query_id=_QUERY_ID,
            query_name=_QUERY_NAME,
            dest_dir=source_dir,
        )
        importer = IBKRImporter(
            account=_ACCOUNT,
            query_name=_QUERY_NAME,
            currency="EUR",
            transactionID_labeled_since=datetime.date(2024, 1, 20),
        )
        return [
            dict(
                name="ibkr",
                importer=importer,
                source_dir=source_dir,
                bean_output_file=bean_output_file,
                setup=setup,
            )
        ]

    def test_labeled_and_unlabeled_cost_specs_in_output(self, tmp_path):
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"

        _run(self._make_labeled_pipeline(src, out))

        text = out.read_text()
        # post-threshold BUYs are labeled: date + transactionID, no price
        assert '{2024-01-22, "784510207"}' in text
        assert '{2024-01-29, "784510208"}' in text
        # pre-threshold BUY keeps the priced cost spec plus the metadata breadcrumb
        assert "{118.43 EUR, 2024-01-15}" in text
        assert 'transactionID: "784510201"' in text
        # closed lots opened pre-threshold stay date-only with the breadcrumb
        assert "{2023-11-12}" in text
        assert 'transactionID: "784510310"' in text

        _, errors, _ = bc_parser.parse_string(text)
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)


# ===========================================================================
# Dry run
# ===========================================================================


class TestDryRun:
    @pytest.fixture(autouse=True)
    def _mock_download(self, mocker):
        mocker.patch.object(fq_module, "_download", return_value=FIXTURE.read_bytes())

    @pytest.fixture(autouse=True)
    def _clean_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

    def test_output_file_not_created(self, tmp_path):
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"
        dry = tmp_path / "dry.bean"

        _run(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))

        assert not out.exists()

    def test_dry_path_written(self, tmp_path):
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"
        dry = tmp_path / "dry.bean"

        _run(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))

        assert dry.exists()
        assert dry.stat().st_size > 0

    def test_dry_output_is_valid_beancount_syntax(self, tmp_path):
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"
        dry = tmp_path / "dry.bean"

        _run(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))

        _, errors, _ = bc_parser.parse_string(dry.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)


# ===========================================================================
# Cache behaviour
# ===========================================================================


class TestCacheHandling:
    @pytest.fixture(autouse=True)
    def _clean_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

    def test_second_run_hits_cache(self, tmp_path, mocker):
        """download is called exactly once; the second run is served from diskcache."""
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"
        mock_dl = mocker.patch.object(fq_module, "_download", return_value=FIXTURE.read_bytes())
        pipeline = _make_pipeline(src, out)

        _run(pipeline)  # first run: cache miss -> download
        out.unlink()  # clear output for re-run
        _run(pipeline)  # second run: cache hit -> no download

        assert mock_dl.call_count == 1

    def test_second_run_produces_valid_syntax(self, tmp_path, mocker):
        """Output from a cache-hit run is still valid beancount syntax."""
        src = tmp_path / "ibkr"
        src.mkdir()
        out = tmp_path / "ibkr.bean"
        mocker.patch.object(fq_module, "_download", return_value=FIXTURE.read_bytes())
        pipeline = _make_pipeline(src, out)

        _run(pipeline)
        out.unlink()
        _run(pipeline)

        _, errors, _ = bc_parser.parse_string(out.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)
