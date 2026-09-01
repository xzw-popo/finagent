import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from finresearch.evidence import (
    copy_evidence_artifacts,
    evidence_record_sha256,
    filter_evidence_as_of,
    load_evidence,
    load_request,
)
from finresearch.schemas import Evidence, EvidenceProvenance

ROOT = Path(__file__).resolve().parents[1]


def _market_evidence(
    raw_artifact_ref: str,
    raw_sha256: str,
    evidence_id: str = "quote-nvda",
) -> Evidence:
    observed_at = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    excerpt = "NVDA.US quote snapshot last price 180.00 USD."
    content_sha256 = hashlib.sha256(excerpt.encode()).hexdigest()
    evidence = Evidence(
        evidence_type="market_quote_snapshot",
        evidence_id=evidence_id,
        title="NVDA.US quote snapshot",
        publisher="Longbridge Securities",
        uri="https://open.longbridge.com",
        locator="cli.quote NVDA.US",
        excerpt=excerpt,
        published_at=None,
        known_at=observed_at,
        retrieved_at=observed_at,
        available_at=observed_at + timedelta(milliseconds=1),
        content_sha256=content_sha256,
        record_sha256="0" * 64,
        provenance=EvidenceProvenance(
            provider="longbridge",
            source_type="market_quote_snapshot",
            source_endpoint="cli.quote",
            symbol="NVDA.US",
            observed_at=observed_at,
            freshness="real_time",
            raw_artifact_ref=raw_artifact_ref,
            raw_sha256=raw_sha256,
            normalized_sha256=content_sha256,
            normalizer_version="longbridge.quote.v1",
        ),
    )
    return evidence.model_copy(
        update={"record_sha256": evidence_record_sha256(evidence)}
    )


def test_future_evidence_is_filtered() -> None:
    request = load_request(ROOT / "examples/request.json")
    evidence = load_evidence(ROOT / "examples/evidence.json")
    eligible, rejected = filter_evidence_as_of(request, evidence)

    assert {item.evidence_id for item in eligible} == {"ev-revenue", "ev-cashflow"}
    assert [(item.evidence_id, item.reason) for item in rejected] == [
        ("ev-future-guidance", "known_at is after as_of")
    ]


def test_metadata_tamper_breaks_record_hash(tmp_path: Path) -> None:
    data = json.loads((ROOT / "examples/evidence.json").read_text())
    data["evidence"][0]["known_at"] = "2026-03-21T08:00:00+08:00"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="record_sha256 mismatch"):
        load_evidence(tampered)


def test_copy_evidence_artifacts_rejects_path_escape(tmp_path: Path) -> None:
    evidence = _market_evidence("../raw-response.json", "a" * 64)

    with pytest.raises(ValueError, match="unsafe raw artifact reference"):
        copy_evidence_artifacts([evidence], tmp_path / "source", tmp_path / "run")


def test_copy_evidence_artifacts_rejects_source_hash_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "raw-response.json").write_bytes(b"actual response")
    evidence = _market_evidence("raw-response.json", "a" * 64)

    with pytest.raises(ValueError, match="raw_sha256 mismatch"):
        copy_evidence_artifacts([evidence], source, tmp_path / "run")


def test_copy_evidence_artifacts_refuses_overwrite_conflict(tmp_path: Path) -> None:
    raw = b'[{"symbol":"NVDA.US","last":"180.00"}]\n'
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    source = tmp_path / "source"
    output = tmp_path / "run"
    source.mkdir()
    output.mkdir()
    (source / "raw-response.json").write_bytes(raw)
    destination = output / "raw-response.json"
    destination.write_bytes(b"unrelated existing file")
    evidence = _market_evidence("raw-response.json", raw_sha256)

    with pytest.raises(ValueError, match="overwrite conflict"):
        copy_evidence_artifacts([evidence], source, output)

    assert destination.read_bytes() == b"unrelated existing file"
