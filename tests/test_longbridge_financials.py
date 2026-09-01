from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import finresearch.fundamentals.longbridge as fundamentals_module
import finresearch.marketdata.longbridge as longbridge_module
from finresearch.evidence import filter_evidence_as_of, load_evidence
from finresearch.evidence_merge import merge_evidence_bundles
from finresearch.fundamentals import (
    LongbridgeFinancialsCollector,
    write_financial_collection,
)
from finresearch.llm.mock import MockAdapter
from finresearch.marketdata import LongbridgeCollectionError
from finresearch.schemas import ResearchRequest
from finresearch.workflow import ResearchWorkflow

ROOT = Path(__file__).resolve().parents[1]

STATEMENT_ARTIFACTS = {
    "IS": "raw/financial-statement-is.json",
    "BS": "raw/financial-statement-bs.json",
    "CF": "raw/financial-statement-cf.json",
}
SEGMENTS_ARTIFACT = "raw/business-segments.json"


def _statement(kind: str, *, report: str = "af", empty: bool = False) -> dict[str, Any]:
    field_by_kind = {
        "IS": ("Total Revenue", "1", "total_rev", "94827000000"),
        "BS": ("Total Assets", "83", "total_assets", "137806000000"),
        "CF": ("Cash from Ops.", "159", "cash_oper", "14747000000"),
    }
    name, field_id, field, value = field_by_kind[kind]
    periods: list[dict[str, Any]] = []
    if not empty:
        periods.append(
            {
                "ff_year": 2025,
                "ff_period": "4",
                "fields": [
                    {
                        "name": name,
                        "id": field_id,
                        "level": 2,
                        "display_order": 1,
                        "value": value,
                        "yoy": "0.125",
                        "field": field,
                        "value_type": "bignumber",
                    }
                ],
                "report_txt": "FY 2025",
                "fp_end": "2025-12-31",
                "rpt_date": "2026-01-28",
            }
        )
    return {
        "currency": "USD" if periods else "",
        "report": report,
        "list": periods,
        "empty_fields": [],
        # Provider responses are forward-compatible. Unreviewed additions must
        # never break the fixed-command boundary or silently become commands.
        "future_provider_field": {"ignored_or_preserved_as_data": True},
    }


def _segments() -> dict[str, Any]:
    return {
        "historical": [
            {
                "total": "94827000000",
                "currency": "USD",
                "date": "20251231",
                "yoy": "-2.93",
                "report": "af",
                "report_txt": "2025",
                "fp_start": "2024.12.31",
                "fp_end": "2025.12.31",
                "rpt_date": "2026.01.28",
                "business": [
                    {
                        "name": "Automotive",
                        "percent": "86.53",
                        "value": "82056000000",
                        "id": "119884",
                        "yoy": "-6.33",
                    }
                ],
                "regionals": [],
                "bus_ids": [],
                "reg_ids": [],
            }
        ],
        "bus_ids": ["119884"],
    }


def _as_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _collector_with_fake_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    statements: dict[str, object] | None = None,
    segments: object | None = None,
) -> tuple[LongbridgeFinancialsCollector, list[tuple[list[str], dict[str, Any]]]]:
    binary = tmp_path / "longbridge"
    binary.write_text("fake executable", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setattr(longbridge_module.shutil, "which", lambda _: str(binary))
    statement_payloads = statements or {
        kind: _statement(kind) for kind in ("IS", "BS", "CF")
    }
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=b"longbridge 0.28.4\n",
                stderr=b"",
            )
        if argv[1] == "financial-statement":
            kind = argv[argv.index("--kind") + 1]
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=_as_bytes(statement_payloads[kind]),
                stderr=b"",
            )
        if argv[1] == "business-segments":
            if segments is None:
                raise AssertionError("business-segments was not expected")
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=_as_bytes(segments),
                stderr=b"",
            )
        raise AssertionError(f"unexpected Longbridge argv: {argv!r}")

    monkeypatch.setattr(longbridge_module, "_bounded_run", fake_run)
    observed_at = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    return LongbridgeFinancialsCollector(clock=lambda: observed_at), calls


def _provider_commands(calls: list[tuple[list[str], dict[str, Any]]]) -> list[list[str]]:
    return [argv[1:] for argv, _kwargs in calls]


def test_collect_financials_uses_three_fixed_commands_and_writes_auditable_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, calls = _collector_with_fake_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-reach-provider")
    monkeypatch.setenv("LONGBRIDGE_ENV", "staging")

    collection = collector.collect("tsla.us", report="af")

    assert _provider_commands(calls) == [
        ["--version"],
        [
            "financial-statement",
            "TSLA.US",
            "--kind",
            "IS",
            "--report",
            "af",
            "--format",
            "json",
        ],
        [
            "financial-statement",
            "TSLA.US",
            "--kind",
            "BS",
            "--report",
            "af",
            "--format",
            "json",
        ],
        [
            "financial-statement",
            "TSLA.US",
            "--kind",
            "CF",
            "--report",
            "af",
            "--format",
            "json",
        ],
    ]
    for _argv, kwargs in calls:
        assert "DEEPSEEK_API_KEY" not in kwargs["env"]
        assert "LONGBRIDGE_ENV" not in kwargs["env"]
        assert kwargs["cwd"]

    assert len(collection.evidence) == 3
    assert {item.evidence_type for item in collection.evidence} == {
        "financial_statement_snapshot"
    }
    assert len({item.evidence_id for item in collection.evidence}) == 3
    by_ref = {
        item.provenance.raw_artifact_ref: item
        for item in collection.evidence
        if item.provenance is not None
    }
    assert set(by_ref) == set(STATEMENT_ARTIFACTS.values())
    for kind, artifact_ref in STATEMENT_ARTIFACTS.items():
        item = by_ref[artifact_ref]
        assert item.publisher == "长桥证券"
        assert item.published_at is None
        assert item.available_at == collection.available_at
        assert item.provenance is not None
        assert item.provenance.source_type == "financial_statement_snapshot"
        assert item.provenance.source_endpoint == "cli.financial-statement"
        assert item.provenance.symbol == "TSLA.US"
        assert item.provenance.source_event_at is None
        assert item.provenance.freshness == "unknown"
        assert item.provenance.normalized_sha256 == item.content_sha256
        assert item.provenance.raw_sha256 == hashlib.sha256(
            _as_bytes(_statement(kind))
        ).hexdigest()
        assert _statement(kind)["list"][0]["fields"][0]["field"] in item.excerpt
        assert '"yoy_ratio":"0.125"' in item.excerpt
        # rpt_date is retained as statement metadata, but it is not promoted to
        # a trusted publication timestamp for the PIT gate.
        assert "2026-01-28" in item.excerpt

    result = write_financial_collection(collection, tmp_path / "financials")
    assert result.evidence_path == tmp_path / "financials/evidence.json"
    assert (tmp_path / "financials/collection.json").is_file()
    loaded = load_evidence(result.evidence_path)
    assert loaded == list(collection.evidence)
    for kind, artifact_ref in STATEMENT_ARTIFACTS.items():
        assert (tmp_path / "financials" / artifact_ref).read_bytes() == _as_bytes(
            _statement(kind)
        )


def test_financial_statement_snapshots_use_available_at_for_pit_and_detect_tampering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(monkeypatch, tmp_path)
    collection = collector.collect("TSLA.US")
    result = write_financial_collection(collection, tmp_path / "financials")
    financials = load_evidence(result.evidence_path)
    document = load_evidence(ROOT / "examples/evidence.json")[0]
    evidence = [document, *financials]
    evidence_ids = [item.evidence_id for item in evidence]

    early_request = ResearchRequest(
        request_id="financials-pit-early",
        question="在财报快照进入系统前，哪些证据可用？",
        universe=["DEMO", "TSLA.US"],
        as_of=collection.available_at - timedelta(microseconds=1),
        horizon="point-in-time",
        allowed_evidence_ids=evidence_ids,
    )
    eligible, rejected = filter_evidence_as_of(early_request, evidence)
    assert [item.evidence_id for item in eligible] == [document.evidence_id]
    assert {(item.evidence_id, item.reason) for item in rejected} == {
        (item.evidence_id, "available_at is after as_of")
        for item in financials
    }

    late_request = early_request.model_copy(
        update={"as_of": collection.available_at + timedelta(microseconds=1)}
    )
    eligible, rejected = filter_evidence_as_of(late_request, evidence)
    assert [item.evidence_id for item in eligible] == evidence_ids
    assert rejected == []

    tampered = tmp_path / "financials" / STATEMENT_ARTIFACTS["IS"]
    tampered.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="raw_sha256 mismatch"):
        load_evidence(result.evidence_path)


def test_financial_bundle_merges_and_runs_through_the_mock_agent_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(monkeypatch, tmp_path)
    collection = collector.collect("TSLA.US")
    financial_result = write_financial_collection(
        collection, tmp_path / "financials"
    )
    merged_result = merge_evidence_bundles(
        [ROOT / "examples/evidence.json", financial_result.evidence_path],
        tmp_path / "merged",
    )
    merged = load_evidence(merged_result.evidence_path)
    financial_evidence = [
        item for item in merged if item.evidence_type == "financial_statement_snapshot"
    ]
    assert len(financial_evidence) == 3
    for item in financial_evidence:
        assert item.provenance is not None
        assert item.provenance.raw_artifact_ref.startswith("artifacts/sha256/")
        assert (tmp_path / "merged" / item.provenance.raw_artifact_ref).is_file()

    request = ResearchRequest(
        request_id="financials-merged-agent-chain",
        question="结合历史文档与完整三表，当前可核验的财务事实是什么？",
        universe=["DEMO", "TSLA.US"],
        as_of=collection.available_at + timedelta(microseconds=1),
        horizon="point-in-time",
        allowed_evidence_ids=[item.evidence_id for item in merged],
    )
    report = ResearchWorkflow(MockAdapter()).run(
        request,
        merged,
        tmp_path / "run",
        evidence_artifact_dir=tmp_path / "merged",
    )

    assert report.stage == "HUMAN_REVIEW_REQUIRED"
    assert set(report.evidence_ids) == {item.evidence_id for item in merged}
    assert len(load_evidence(tmp_path / "run/eligible_evidence.json")) == len(merged)


def test_include_segments_adds_one_fixed_read_only_command_and_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segment_payload = _segments()
    collector, calls = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        segments=segment_payload,
    )

    collection = collector.collect("TSLA.US", include_segments=True)

    assert _provider_commands(calls)[-1] == [
        "business-segments",
        "TSLA.US",
        "--history",
        "--report",
        "af",
        "--format",
        "json",
    ]
    assert len(collection.evidence) == 4
    segment_items = [
        item
        for item in collection.evidence
        if item.evidence_type == "business_segment_snapshot"
    ]
    assert len(segment_items) == 1
    segment = segment_items[0]
    assert segment.provenance is not None
    assert segment.provenance.source_type == "business_segment_snapshot"
    assert segment.provenance.source_endpoint == "cli.business-segments"
    assert segment.provenance.raw_artifact_ref == SEGMENTS_ARTIFACT
    assert segment.provenance.raw_sha256 == hashlib.sha256(
        _as_bytes(segment_payload)
    ).hexdigest()
    assert "Automotive" in segment.excerpt
    assert '"yoy_percent":"-6.33"' in segment.excerpt
    assert segment.published_at is None
    assert segment.available_at == collection.available_at

    result = write_financial_collection(collection, tmp_path / "financials")
    assert (tmp_path / "financials" / SEGMENTS_ARTIFACT).read_bytes() == _as_bytes(
        segment_payload
    )
    assert load_evidence(result.evidence_path) == list(collection.evidence)


def test_segments_are_not_called_or_materialized_unless_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, calls = _collector_with_fake_cli(monkeypatch, tmp_path)

    collection = collector.collect("TSLA.US", include_segments=False)
    result = write_financial_collection(collection, tmp_path / "financials")

    assert all(argv[1] != "business-segments" for argv, _kwargs in calls[1:])
    assert not (tmp_path / "financials" / SEGMENTS_ARTIFACT).exists()
    assert all(
        item.evidence_type == "financial_statement_snapshot"
        for item in load_evidence(result.evidence_path)
    )


@pytest.mark.parametrize(
    ("empty_kinds", "expected_code"),
    [
        ({"IS"}, "partial_result"),
        ({"IS", "BS", "CF"}, "no_data"),
    ],
)
def test_empty_financial_statements_are_classified_without_silent_partial_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_kinds: set[str],
    expected_code: str,
) -> None:
    statements = {
        kind: _statement(kind, empty=kind in empty_kinds)
        for kind in ("IS", "BS", "CF")
    }
    collector, _calls = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        statements=statements,
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US")

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    "invalid_payload",
    [
        [],
        {"currency": "USD", "report": "af", "list": {}},
        {
            "currency": "USD",
            "report": "af",
            "list": [{"ff_year": 2025, "fields": "not-an-array"}],
        },
    ],
)
def test_financial_statement_schema_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_payload: object,
) -> None:
    statements: dict[str, object] = {
        "IS": invalid_payload,
        "BS": _statement("BS"),
        "CF": _statement("CF"),
    }
    collector, _calls = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        statements=statements,
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US")

    assert error.value.code == "schema_mismatch"


def test_extreme_decimal_exponents_are_rejected_before_string_expansion() -> None:
    with pytest.raises(LongbridgeCollectionError) as error:
        fundamentals_module._optional_text(Decimal("1e1000000000"), "value")

    assert error.value.code == "schema_mismatch"


def test_oversized_integer_strings_stay_inside_the_schema_error_boundary() -> None:
    with pytest.raises(LongbridgeCollectionError) as error:
        fundamentals_module._required_integer("9" * 5_000, "level")

    assert error.value.code == "schema_mismatch"


def test_complete_bundle_rejects_misaligned_latest_periods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    statements = {kind: _statement(kind) for kind in ("IS", "BS", "CF")}
    statements["BS"]["list"][0]["ff_year"] = 2024
    collector, _calls = _collector_with_fake_cli(
        monkeypatch, tmp_path, statements=statements
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US")

    assert error.value.code == "protocol_mismatch"


def test_complete_bundle_rejects_misaligned_currencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    statements = {kind: _statement(kind) for kind in ("IS", "BS", "CF")}
    statements["CF"]["currency"] = "EUR"
    collector, _calls = _collector_with_fake_cli(
        monkeypatch, tmp_path, statements=statements
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US")

    assert error.value.code == "protocol_mismatch"


def test_complete_bundle_rejects_cumulative_report_before_running_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, calls = _collector_with_fake_cli(monkeypatch, tmp_path)

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US", report="cumul")  # type: ignore[arg-type]

    assert error.value.code == "invalid_report"
    assert calls == []


def test_requested_segments_with_invalid_schema_fail_the_whole_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        segments={"historical": "not-an-array", "bus_ids": []},
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US", include_segments=True)

    assert error.value.code == "schema_mismatch"


def test_requested_but_empty_segments_are_a_partial_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        segments={"historical": [], "bus_ids": []},
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US", include_segments=True)

    assert error.value.code == "partial_result"


def test_requested_segment_period_without_breakdown_is_a_partial_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segment_payload = _segments()
    segment_payload["historical"][0]["business"] = []
    collector, _calls = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        segments=segment_payload,
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US", include_segments=True)

    assert error.value.code == "partial_result"


def test_financial_writer_refuses_existing_directory_and_dangling_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(monkeypatch, tmp_path)
    collection = collector.collect("TSLA.US")
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_financial_collection(collection, existing)

    assert sentinel.read_text(encoding="utf-8") == "keep"

    dangling = tmp_path / "dangling"
    target = tmp_path / "missing-target"
    dangling.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        write_financial_collection(collection, dangling)

    assert os.path.lexists(dangling)
    assert dangling.is_symlink()
    assert not target.exists()


def test_financial_writer_replays_raw_normalization_before_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(monkeypatch, tmp_path)
    collection = collector.collect("TSLA.US")
    original_period = collection.statements[0].periods[0]
    original_field = original_period.fields[0]
    tampered_field = original_field.model_copy(update={"value": "999"})
    tampered_period = original_period.model_copy(
        update={"fields": (tampered_field, *original_period.fields[1:])}
    )
    tampered_statement = collection.statements[0].model_copy(
        update={"periods": (tampered_period, *collection.statements[0].periods[1:])}
    )
    artifact = collection.artifacts[0]
    tampered_evidence = fundamentals_module._build_snapshot_evidence(
        tampered_statement,
        symbol=collection.symbol,
        cli_version=collection.cli_version,
        retrieved_at=artifact.retrieved_at,
        available_at=collection.available_at,
        artifact=artifact,
    )
    forged = replace(
        collection,
        statements=(tampered_statement, *collection.statements[1:]),
        evidence=(tampered_evidence, *collection.evidence[1:]),
    )

    with pytest.raises(ValueError, match="normalization does not match"):
        write_financial_collection(forged, tmp_path / "forged")

    assert not (tmp_path / "forged").exists()


def test_financial_writer_classifies_empty_statement_periods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(monkeypatch, tmp_path)
    collection = collector.collect("TSLA.US")
    empty_statement = collection.statements[0].model_copy(update={"periods": ()})
    malformed = replace(
        collection,
        statements=(empty_statement, *collection.statements[1:]),
    )

    with pytest.raises(ValueError, match="statement set is invalid"):
        write_financial_collection(malformed, tmp_path / "empty-periods")

    assert not (tmp_path / "empty-periods").exists()


def test_financial_evidence_must_fit_default_merge_input_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        fundamentals_module, "_MAX_FINANCIAL_EVIDENCE_BUNDLE_BYTES", 1
    )
    collector, _calls = _collector_with_fake_cli(monkeypatch, tmp_path)

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect("TSLA.US")

    assert error.value.code == "output_too_large"


def test_financial_writer_rechecks_default_merge_input_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _calls = _collector_with_fake_cli(monkeypatch, tmp_path)
    collection = collector.collect("TSLA.US")
    monkeypatch.setattr(
        fundamentals_module, "_MAX_FINANCIAL_EVIDENCE_BUNDLE_BYTES", 1
    )
    output = tmp_path / "oversized-evidence"

    with pytest.raises(ValueError, match="default merge input limit"):
        write_financial_collection(collection, output)

    assert not output.exists()
