import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from finresearch.evidence import (
    evidence_record_sha256,
    filter_evidence_as_of,
    load_evidence,
)
from finresearch.evidence_merge import merge_evidence_bundles
from finresearch.llm.mock import MockAdapter
from finresearch.schemas import (
    Evidence,
    EvidenceBundle,
    EvidenceProvenance,
    ResearchRequest,
)
from finresearch.workflow import ResearchWorkflow

ROOT = Path(__file__).resolve().parents[1]


def _market_evidence(raw: bytes, available_at: datetime) -> Evidence:
    excerpt = "NVDA.US normalized market quote snapshot last price 180.00 USD."
    content_sha256 = hashlib.sha256(excerpt.encode()).hexdigest()
    evidence = Evidence(
        evidence_type="market_quote_snapshot",
        evidence_id="ev-nvda-quote",
        title="NVDA.US quote snapshot",
        publisher="Longbridge Securities",
        uri="https://open.longbridge.com",
        locator="cli.quote NVDA.US",
        excerpt=excerpt,
        published_at=None,
        known_at=available_at - timedelta(milliseconds=1),
        retrieved_at=available_at - timedelta(milliseconds=1),
        available_at=available_at,
        content_sha256=content_sha256,
        record_sha256="0" * 64,
        provenance=EvidenceProvenance(
            provider="longbridge",
            source_type="market_quote_snapshot",
            source_endpoint="cli.quote",
            symbol="NVDA.US",
            observed_at=available_at - timedelta(milliseconds=2),
            freshness="unknown",
            raw_artifact_ref="raw-response.json",
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            normalized_sha256=content_sha256,
            normalizer_version="longbridge.quote.v1",
        ),
    )
    return evidence.model_copy(
        update={"record_sha256": evidence_record_sha256(evidence)}
    )


def test_merge_validate_and_mock_run_are_one_self_contained_chain(
    tmp_path: Path,
) -> None:
    document = load_evidence(ROOT / "examples/evidence.json")[0]
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    (documents_dir / "evidence.json").write_text(
        EvidenceBundle(evidence=[document]).model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    raw = b'[{"symbol":"NVDA.US","last":"180.00"}]\n'
    available_at = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    quotes_dir = tmp_path / "quotes"
    quotes_dir.mkdir()
    (quotes_dir / "raw-response.json").write_bytes(raw)
    market = _market_evidence(raw, available_at)
    (quotes_dir / "evidence.json").write_text(
        EvidenceBundle(evidence=[market]).model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    merged_dir = tmp_path / "merged"
    result = merge_evidence_bundles(
        [documents_dir / "evidence.json", quotes_dir / "evidence.json"],
        merged_dir,
    )
    merged = load_evidence(result.evidence_path)
    evidence_ids = [item.evidence_id for item in merged]

    early_request = ResearchRequest(
        request_id="merged-pit-early",
        question="在早于行情可用时点的研究中，哪些证据可用？",
        universe=["DEMO", "NVDA.US"],
        as_of=available_at - timedelta(seconds=1),
        horizon="point-in-time",
        allowed_evidence_ids=evidence_ids,
    )
    eligible, rejected = filter_evidence_as_of(early_request, merged)
    assert [item.evidence_id for item in eligible] == [document.evidence_id]
    assert [(item.evidence_id, item.reason) for item in rejected] == [
        (market.evidence_id, "available_at is after as_of")
    ]

    request = ResearchRequest(
        request_id="merged-end-to-end",
        question="结合已核验文档和行情快照，当前证据显示了什么？",
        universe=["DEMO", "NVDA.US"],
        as_of=available_at + timedelta(seconds=1),
        horizon="point-in-time",
        allowed_evidence_ids=evidence_ids,
    )
    run_dir = tmp_path / "run"
    report = ResearchWorkflow(MockAdapter()).run(
        request,
        merged,
        run_dir,
        evidence_artifact_dir=merged_dir,
    )

    assert report.stage == "HUMAN_REVIEW_REQUIRED"
    assert set(report.evidence_ids) == set(evidence_ids)
    reloaded = load_evidence(run_dir / "eligible_evidence.json")
    assert [item.evidence_id for item in reloaded] == evidence_ids
    digest = hashlib.sha256(raw).hexdigest()
    assert (run_dir / "artifacts" / "sha256" / digest).read_bytes() == raw
