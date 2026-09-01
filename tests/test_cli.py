from __future__ import annotations

import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import finresearch.cli as cli_module
from finresearch import __version__
from finresearch.marketdata import LongbridgeCollectionError


def _run_main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["finresearch", *arguments])
    cli_module.main()


def test_collect_quote_invalid_symbol_remains_argument_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "collect-quote",
            "NVDA",
            "--output",
            str(tmp_path / "quotes"),
        )

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "usage:" in captured.err
    assert "invalid_symbol" in captured.err


@pytest.mark.parametrize(
    ("provider_code", "retryable", "expected_status"),
    [
        ("timeout", True, 4),
        ("authentication_required", False, 5),
        ("no_data", True, 6),
        ("partial_result", False, 7),
    ],
)
def test_collect_quote_runtime_errors_have_stable_exit_codes_without_usage(
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

        def collect(self, _symbols: list[str]) -> object:
            raise LongbridgeCollectionError(
                provider_code,
                "provider failure",
                retryable=retryable,
            )

    monkeypatch.setattr(cli_module, "LongbridgeQuoteCollector", FailingCollector)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "collect-quote",
            "NVDA.US",
            "--output",
            str(tmp_path / "quotes"),
        )

    captured = capsys.readouterr()
    assert error.value.code == expected_status
    assert f"code={provider_code}" in captured.err
    assert f"retryable={str(retryable).lower()}" in captured.err
    assert "usage:" not in captured.err


def test_collect_quote_local_output_error_exits_8_without_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class SuccessfulCollector:
        def __init__(self, _config: object) -> None:
            pass

        def collect(self, _symbols: list[str]) -> object:
            return SimpleNamespace(
                evidence=(SimpleNamespace(evidence_id="lbq-test"),),
                available_at=datetime(2026, 9, 1, tzinfo=UTC),
            )

    def fail_to_write(_collection: object, _output: Path) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(cli_module, "LongbridgeQuoteCollector", SuccessfulCollector)
    monkeypatch.setattr(cli_module, "write_quote_collection", fail_to_write)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "collect-quote",
            "NVDA.US",
            "--output",
            str(tmp_path / "quotes"),
        )

    captured = capsys.readouterr()
    assert error.value.code == 8
    assert "local output error" in captured.err
    assert "usage:" not in captured.err


def test_collect_quote_success_prints_required_source_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_path = tmp_path / "quotes" / "evidence.json"

    class SuccessfulCollector:
        def __init__(self, _config: object) -> None:
            pass

        def collect(self, _symbols: list[str]) -> object:
            return SimpleNamespace(
                evidence=(SimpleNamespace(evidence_id="lbq-test"),),
                available_at=datetime(2026, 9, 1, tzinfo=UTC),
            )

    monkeypatch.setattr(cli_module, "LongbridgeQuoteCollector", SuccessfulCollector)
    monkeypatch.setattr(
        cli_module,
        "write_quote_collection",
        lambda _collection, _output: evidence_path,
    )

    _run_main(
        monkeypatch,
        "collect-quote",
        "NVDA.US",
        "--output",
        str(tmp_path / "quotes"),
    )

    captured = capsys.readouterr()
    assert "数据来源：长桥证券" in captured.out
    assert "available_at: 2026-09-01T00:00:00+00:00" in captured.out
    assert "evidence_id: lbq-test" in captured.out
    assert captured.err == ""


def test_merge_evidence_requires_at_least_two_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "merge-evidence",
            "--evidence",
            str(tmp_path / "one/evidence.json"),
            "--output",
            str(tmp_path / "merged"),
        )

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "usage:" in captured.err
    assert "at least two --evidence inputs" in captured.err


@pytest.mark.parametrize(
    ("merge_code", "expected_status"),
    [
        ("output_error", 8),
        ("invalid_bundle", 9),
        ("input_error", 9),
        ("duplicate_evidence_id", 10),
        ("resource_limit", 11),
        ("unexpected_merge_error", 9),
    ],
)
def test_merge_evidence_runtime_errors_have_stable_exit_codes_without_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    merge_code: str,
    expected_status: int,
) -> None:
    class FakeMergeError(Exception):
        def __init__(self, code: str) -> None:
            super().__init__("merge failed")
            self.code = code

    def fail_to_merge(
        _inputs: list[Path], _output: Path, *, limits: object
    ) -> object:
        assert isinstance(limits, cli_module.MergeLimits)
        raise FakeMergeError(merge_code)

    monkeypatch.setattr(cli_module, "EvidenceMergeError", FakeMergeError)
    monkeypatch.setattr(cli_module, "merge_evidence_bundles", fail_to_merge)

    with pytest.raises(SystemExit) as error:
        _run_main(
            monkeypatch,
            "merge-evidence",
            "--evidence",
            str(tmp_path / "one/evidence.json"),
            "--evidence",
            str(tmp_path / "two/evidence.json"),
            "--output",
            str(tmp_path / "merged"),
        )

    captured = capsys.readouterr()
    assert error.value.code == expected_status
    assert f"code={merge_code}" in captured.err
    assert "usage:" not in captured.err
    assert "Traceback" not in captured.err


def test_merge_evidence_success_prints_paths_pit_time_and_sorted_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "merged"
    evidence_path = output_dir / "evidence.json"
    manifest_path = output_dir / "merge.json"
    minimum_as_of = datetime(2026, 9, 1, 8, 12, 34, tzinfo=UTC)
    captured_call: dict[str, object] = {}

    def merge(
        inputs: list[Path], output: Path, *, limits: object
    ) -> SimpleNamespace:
        captured_call.update(inputs=inputs, output=output, limits=limits)
        return SimpleNamespace(
            evidence_path=evidence_path,
            manifest_path=manifest_path,
            evidence_ids=("ev-z", "ev-a"),
            minimum_as_of_for_all_evidence=minimum_as_of,
            input_count=2,
            evidence_count=2,
            artifact_count=1,
        )

    monkeypatch.setattr(cli_module, "merge_evidence_bundles", merge)

    first = tmp_path / "one/evidence.json"
    second = tmp_path / "two/evidence.json"
    _run_main(
        monkeypatch,
        "merge-evidence",
        "--evidence",
        str(first),
        "--evidence",
        str(second),
        "--output",
        str(output_dir),
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured_call["inputs"] == [first, second]
    assert captured_call["output"] == output_dir
    assert isinstance(captured_call["limits"], cli_module.MergeLimits)
    assert "merged: 2 evidence record(s) from 2 bundle(s)" in captured.out
    assert f"evidence: {evidence_path.resolve()}" in captured.out
    assert f"manifest: {manifest_path.resolve()}" in captured.out
    assert (
        "minimum_as_of_for_all_evidence: 2026-09-01T08:12:34+00:00"
        in captured.out
    )
    assert captured.out.index("evidence_id: ev-a") < captured.out.index(
        "evidence_id: ev-z"
    )


def test_merge_success_escapes_terminal_controls_and_reports_durability_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "merged"
    malicious_id = "evil\nmerged: 999 forged record(s)\x1b[31m"
    monkeypatch.setattr(
        cli_module,
        "merge_evidence_bundles",
        lambda *_args, **_kwargs: SimpleNamespace(
            evidence_path=output_dir / "evidence.json",
            manifest_path=output_dir / "merge.json",
            evidence_ids=(malicious_id,),
            minimum_as_of_for_all_evidence=datetime(2026, 9, 1, tzinfo=UTC),
            input_count=2,
            evidence_count=1,
            artifact_count=0,
            durability_confirmed=False,
        ),
    )

    _run_main(
        monkeypatch,
        "merge-evidence",
        "--evidence",
        str(tmp_path / "one.json"),
        "--evidence",
        str(tmp_path / "two.json"),
        "--output",
        str(output_dir),
    )

    captured = capsys.readouterr()
    assert "\nmerged: 999 forged" not in captured.out
    assert "evidence_id: evil\\u000amerged: 999 forged record(s)\\u001b[31m" in captured.out
    assert "crash durability is unconfirmed" in captured.err


def test_merge_evidence_cli_runs_real_merge_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = json.loads(
        (Path(__file__).parents[1] / "examples/evidence.json").read_text(
            encoding="utf-8"
        )
    )
    first = tmp_path / "one/evidence.json"
    second = tmp_path / "two/evidence.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(
        json.dumps({"evidence": [source["evidence"][0]]}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"evidence": [source["evidence"][1]]}),
        encoding="utf-8",
    )
    output_dir = tmp_path / "merged"

    _run_main(
        monkeypatch,
        "merge-evidence",
        "--evidence",
        str(first),
        "--evidence",
        str(second),
        "--output",
        str(output_dir),
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "merged: 2 evidence record(s) from 2 bundle(s)" in captured.out
    assert (output_dir / "evidence.json").is_file()
    assert (output_dir / "merge.json").is_file()
    assert captured.out.index("evidence_id: ev-cashflow") < captured.out.index(
        "evidence_id: ev-revenue"
    )


def test_merge_evidence_help_documents_repeatable_inputs_and_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        _run_main(monkeypatch, "merge-evidence", "--help")

    captured = capsys.readouterr()
    normalized_help = " ".join(captured.out.split())
    assert error.value.code == 0
    assert "repeat this option at least twice" in normalized_help
    assert "path must not already exist" in normalized_help
    assert "stable exit codes:" in captured.out
    assert "10 duplicate evidence_id" in captured.out
    assert "11 resource limit exceeded" in captured.out
    assert captured.err == ""


def test_cli_version_matches_package_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        _run_main(monkeypatch, "--version")

    captured = capsys.readouterr()
    assert error.value.code == 0
    assert captured.out.strip() == f"finresearch {__version__}"


def test_package_version_matches_project_version() -> None:
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    assert __version__ == project["project"]["version"] == "0.4.0"
