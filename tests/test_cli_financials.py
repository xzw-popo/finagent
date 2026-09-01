from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import finresearch.cli as cli_module
from finresearch.marketdata import LongbridgeCollectionError


def _run_main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["finresearch", *arguments])
    cli_module.main()


def test_collect_financials_passes_bounded_config_and_collection_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "financials"
    evidence_path = output_dir / "evidence.json"
    collection = SimpleNamespace(
        evidence=(
            SimpleNamespace(evidence_id="lbf-nvda-us-qf-statements"),
            SimpleNamespace(evidence_id="lbf-nvda-us-qf-segments"),
        ),
        available_at=datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
    )
    captured_call: dict[str, object] = {}

    class CapturingConfig:
        def __init__(self, **kwargs: object) -> None:
            captured_call["config"] = kwargs

    class CapturingCollector:
        def __init__(self, config: object) -> None:
            captured_call["collector_config"] = config

        def collect(
            self,
            symbol: str,
            *,
            report: str,
            include_segments: bool,
        ) -> object:
            captured_call.update(
                symbol=symbol,
                report=report,
                include_segments=include_segments,
            )
            return collection

    def write(collected: object, output: Path) -> SimpleNamespace:
        captured_call.update(collection=collected, output=output)
        return SimpleNamespace(
            evidence_path=evidence_path,
            manifest_path=output_dir / "collection.json",
            durability_confirmed=True,
        )

    monkeypatch.setattr(cli_module, "FinancialsCollectorConfig", CapturingConfig)
    monkeypatch.setattr(
        cli_module,
        "LongbridgeFinancialsCollector",
        CapturingCollector,
    )
    monkeypatch.setattr(cli_module, "write_financial_collection", write)

    _run_main(
        monkeypatch,
        "collect-financials",
        "nvda.us",
        "--report",
        "qf",
        "--segments",
        "--output",
        str(output_dir),
        "--timeout",
        "12.5",
        "--region",
        "global",
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured_call["config"] == {
        "timeout_seconds": 12.5,
        "region": "global",
    }
    assert isinstance(captured_call["collector_config"], CapturingConfig)
    assert captured_call["symbol"] == "nvda.us"
    assert captured_call["report"] == "qf"
    assert captured_call["include_segments"] is True
    assert captured_call["collection"] is collection
    assert captured_call["output"] == output_dir
    assert f"evidence: {evidence_path.resolve()}" in captured.out


@pytest.mark.parametrize(
    ("provider_code", "retryable", "expected_status"),
    [
        ("timeout", True, 4),
        ("authentication_required", False, 5),
        ("schema_mismatch", False, 5),
        ("no_data", True, 6),
        ("partial_result", False, 7),
    ],
)
def test_collect_financials_runtime_errors_have_stable_exit_codes_without_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider_code: str,
    retryable: bool,
    expected_status: int,
) -> None:
    class FailingCollector:
        def __init__(self, _config: object) -> None:
            pass

        def collect(
            self,
            _symbol: str,
            *,
            report: str,
            include_segments: bool,
        ) -> object:
            assert report == "af"
            assert include_segments is False
            raise LongbridgeCollectionError(
                provider_code,
                "provider failure",
                retryable=retryable,
            )

    monkeypatch.setattr(
        cli_module,
        "LongbridgeFinancialsCollector",
        FailingCollector,
    )

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "collect-financials",
            "NVDA.US",
            "--report",
            "af",
            "--output",
            str(tmp_path / "financials"),
        )

    captured = capsys.readouterr()
    assert error.value.code == expected_status
    assert f"finresearch collect-financials: error code={provider_code}" in captured.err
    assert f"retryable={str(retryable).lower()}" in captured.err
    assert "usage:" not in captured.err
    assert "Traceback" not in captured.err


def test_collect_financials_invalid_symbol_remains_argument_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class RejectingCollector:
        def __init__(self, _config: object) -> None:
            pass

        def collect(self, *_args: object, **_kwargs: object) -> object:
            raise LongbridgeCollectionError(
                "invalid_symbol",
                "symbol must use an explicit supported suffix",
            )

    monkeypatch.setattr(
        cli_module,
        "LongbridgeFinancialsCollector",
        RejectingCollector,
    )

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "collect-financials",
            "NVDA",
            "--output",
            str(tmp_path / "financials"),
        )

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "usage:" in captured.err
    assert "invalid_symbol" in captured.err


def test_collect_financials_local_output_error_exits_8_without_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collection = SimpleNamespace(
        evidence=(SimpleNamespace(evidence_id="lbf-test"),),
        available_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    class SuccessfulCollector:
        def __init__(self, _config: object) -> None:
            pass

        def collect(self, *_args: object, **_kwargs: object) -> object:
            return collection

    def fail_to_write(_collection: object, _output: Path) -> object:
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        cli_module,
        "LongbridgeFinancialsCollector",
        SuccessfulCollector,
    )
    monkeypatch.setattr(cli_module, "write_financial_collection", fail_to_write)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "collect-financials",
            "NVDA.US",
            "--output",
            str(tmp_path / "financials"),
        )

    captured = capsys.readouterr()
    assert error.value.code == 8
    assert "finresearch collect-financials: local output error" in captured.err
    assert "usage:" not in captured.err
    assert "Traceback" not in captured.err


def test_collect_financials_success_prints_source_time_and_evidence_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence_path = tmp_path / "financials" / "evidence.json"
    collection = SimpleNamespace(
        evidence=(
            SimpleNamespace(evidence_id="lbf-statements"),
            SimpleNamespace(evidence_id="lbf-segments"),
        ),
        available_at=datetime(2026, 9, 1, 8, 30, 45, tzinfo=UTC),
    )

    class SuccessfulCollector:
        def __init__(self, _config: object) -> None:
            pass

        def collect(self, *_args: object, **_kwargs: object) -> object:
            return collection

    monkeypatch.setattr(
        cli_module,
        "LongbridgeFinancialsCollector",
        SuccessfulCollector,
    )
    monkeypatch.setattr(
        cli_module,
        "write_financial_collection",
        lambda _collection, _output: SimpleNamespace(
            evidence_path=evidence_path,
            manifest_path=tmp_path / "financials" / "collection.json",
            durability_confirmed=True,
        ),
    )

    _run_main(
        monkeypatch,
        "collect-financials",
        "NVDA.US",
        "--segments",
        "--output",
        str(tmp_path / "financials"),
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "available_at: 2026-09-01T08:30:45+00:00" in captured.out
    assert "evidence_id: lbf-statements" in captured.out
    assert "evidence_id: lbf-segments" in captured.out
    assert "数据来源：长桥证券" in captured.out


def test_collect_financials_rejects_segments_with_cumulative_report_before_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class MustNotConstructCollector:
        def __init__(self, _config: object) -> None:
            raise AssertionError("collector must not run for an invalid option combination")

    monkeypatch.setattr(
        cli_module,
        "LongbridgeFinancialsCollector",
        MustNotConstructCollector,
    )

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "collect-financials",
            "NVDA.US",
            "--report",
            "cumul",
            "--segments",
            "--output",
            str(tmp_path / "financials"),
        )

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "usage: finresearch collect-financials" in captured.err
    assert "segments" in captured.err.lower()
    assert "cumul" in captured.err.lower()
    assert "Traceback" not in captured.err


def test_collect_financials_help_explains_report_codes_and_cumulative_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        _run_main(monkeypatch, "collect-financials", "--help")

    captured = capsys.readouterr()
    normalized = " ".join(captured.out.split())
    assert error.value.code == 0
    assert "af=annual" in normalized
    assert "saf=semi-annual" in normalized
    assert "qf=quarterly" in normalized
    assert "cumul is intentionally unsupported" in normalized
    assert captured.err == ""
