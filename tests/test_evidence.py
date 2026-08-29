import json
from pathlib import Path

import pytest

from finresearch.evidence import filter_evidence_as_of, load_evidence, load_request

ROOT = Path(__file__).resolve().parents[1]


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
