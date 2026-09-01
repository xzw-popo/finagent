import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from finresearch.evidence import evidence_record_sha256, load_evidence, load_request
from finresearch.llm.mock import MockAdapter
from finresearch.schemas import (
    Claim,
    Evidence,
    EvidenceBundle,
    EvidenceProvenance,
    GroundedStatement,
    ResearchRequest,
)
from finresearch.workflow import (
    ResearchWorkflow,
    Stage,
    WorkflowStateMachine,
    _validate_grounding,
)

ROOT = Path(__file__).resolve().parents[1]


class RecordingMockAdapter(MockAdapter):
    def __init__(self) -> None:
        self.payloads = []

    def generate(self, task, payload, output_model):
        self.payloads.append(payload)
        return super().generate(task, payload, output_model)


def _quote_evidence(
    symbol: str,
    index: int,
    raw_sha256: str,
    observed_at: datetime,
    raw_artifact_ref: str = "raw/quote-response.json",
) -> Evidence:
    excerpt = f"{symbol} quote snapshot last price {180 + index}.00 USD."
    content_sha256 = hashlib.sha256(excerpt.encode()).hexdigest()
    evidence = Evidence(
        evidence_type="market_quote_snapshot",
        evidence_id=f"quote-{index}",
        title=f"{symbol} quote snapshot",
        publisher="Longbridge Securities",
        uri="https://open.longbridge.com",
        locator=f"cli.quote {symbol}",
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
            symbol=symbol,
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


def test_state_machine_rejects_skipped_gate() -> None:
    machine = WorkflowStateMachine()
    with pytest.raises(RuntimeError, match="illegal workflow transition"):
        machine.transition(Stage.EXTRACT_CLAIMS)


def test_mock_workflow_is_auditable_and_requires_human_review(tmp_path: Path) -> None:
    request = load_request(ROOT / "examples/request.json")
    evidence = load_evidence(ROOT / "examples/evidence.json")
    run_dir = tmp_path / "run"

    adapter = RecordingMockAdapter()
    report = ResearchWorkflow(adapter).run(request, evidence, run_dir)

    assert report.stage == "HUMAN_REVIEW_REQUIRED"
    assert report.human_review_required is True
    assert report.narrative_requires_human_verification is True
    assert report.executive_summary.trust_status == "UNVERIFIED_NARRATIVE"
    assert report.rejected_evidence_ids == ["ev-future-guidance"]
    assert "ev-future-guidance" not in report.evidence_ids
    assert len(report.verified_claims) == 2
    assert all("ev-future-guidance" not in json.dumps(payload) for payload in adapter.payloads)
    assert (run_dir / "report.json").exists()
    assert {item.evidence_id for item in load_evidence(run_dir / "eligible_evidence.json")} == {
        "ev-revenue",
        "ev-cashflow",
    }

    event_lines = (run_dir / "events.jsonl").read_text().splitlines()
    stages = [json.loads(line)["stage"] for line in event_lines]
    assert stages == [stage.value for stage in Stage]


def test_mock_workflow_supports_a_single_evidence_record(tmp_path: Path) -> None:
    evidence = load_evidence(ROOT / "examples/evidence.json")[:1]
    request = ResearchRequest(
        request_id="single-evidence-test",
        question="单条证据能否完整通过受控研究工作流？",
        universe=["EXAMPLE"],
        as_of=datetime(2026, 8, 29, tzinfo=UTC),
        horizon="point-in-time",
        allowed_evidence_ids=[evidence[0].evidence_id],
    )

    report = ResearchWorkflow(MockAdapter()).run(
        request, evidence, tmp_path / "single-run"
    )

    assert [claim.evidence_ids for claim in report.verified_claims] == [
        [evidence[0].evidence_id]
    ]
    assert report.executive_summary.claim_ids == ["claim-1"]


def test_workflow_refuses_existing_symlink_output_before_llm(tmp_path: Path) -> None:
    request = load_request(ROOT / "examples/request.json")
    evidence = load_evidence(ROOT / "examples/evidence.json")
    victim = tmp_path / "victim.txt"
    victim.write_text("DO-NOT-TOUCH", encoding="utf-8")
    output = tmp_path / "run"
    output.symlink_to(victim)
    adapter = RecordingMockAdapter()

    with pytest.raises(FileExistsError, match="output path already exists"):
        ResearchWorkflow(adapter).run(request, evidence, output)

    assert victim.read_text(encoding="utf-8") == "DO-NOT-TOUCH"
    assert adapter.payloads == []


def test_workflow_rejects_casefolded_reserved_raw_artifact_path(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "collected"
    source_dir.mkdir()
    raw = b"provider response"
    (source_dir / "REQUEST.JSON").write_bytes(raw)
    observed_at = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    evidence = [
        _quote_evidence(
            "NVDA.US",
            1,
            hashlib.sha256(raw).hexdigest(),
            observed_at,
            raw_artifact_ref="REQUEST.JSON",
        )
    ]
    request = ResearchRequest(
        request_id="reserved-artifact-test",
        question="受保护的工作流输出路径是否会被原始证据覆盖？",
        universe=["NVDA.US"],
        as_of=observed_at + timedelta(seconds=1),
        horizon="point-in-time",
        allowed_evidence_ids=[evidence[0].evidence_id],
    )
    adapter = RecordingMockAdapter()

    with pytest.raises(ValueError, match="conflicts with workflow output"):
        ResearchWorkflow(adapter).run(
            request,
            evidence,
            tmp_path / "run",
            evidence_artifact_dir=source_dir,
        )

    assert adapter.payloads == []


def test_report_grounding_rejects_non_verified_claims() -> None:
    claim = Claim(
        claim_id="claim-insufficient",
        kind="fact",
        text="证据不足以支持该项财务结论。",
        evidence_ids=["ev-1"],
        as_of=datetime(2026, 8, 29, tzinfo=UTC),
        confidence=0.2,
        status="insufficient",
        verifier_notes="缺少可核验的原始证据。",
    )
    statement = GroundedStatement(
        text="这条叙述不应被当成已核验结论。",
        claim_ids=[claim.claim_id],
    )

    with pytest.raises(ValueError, match="disallowed status"):
        _validate_grounding(
            statement,
            {claim.claim_id: claim},
            "executive summary",
            {"verified"},
        )


def test_market_evidence_workflow_output_is_self_contained(tmp_path: Path) -> None:
    source_dir = tmp_path / "collected"
    raw_path = source_dir / "raw" / "quote-response.json"
    raw_path.parent.mkdir(parents=True)
    raw = (
        b'[{"symbol":"NVDA.US","last":"180.00"},'
        b'{"symbol":"AMD.US","last":"181.00"}]\n'
    )
    raw_path.write_bytes(raw)
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    observed_at = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    evidence = [
        _quote_evidence("NVDA.US", 1, raw_sha256, observed_at),
        _quote_evidence("AMD.US", 2, raw_sha256, observed_at),
    ]
    collected_bundle = source_dir / "evidence.json"
    collected_bundle.write_text(
        EvidenceBundle(evidence=evidence).model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    loaded = load_evidence(collected_bundle)
    request = ResearchRequest(
        request_id="market-workflow-test",
        question="截至指定时点，两只股票的行情快照分别是什么？",
        universe=["NVDA.US", "AMD.US"],
        as_of=observed_at + timedelta(seconds=1),
        horizon="point-in-time",
        allowed_evidence_ids=[item.evidence_id for item in evidence],
    )
    run_dir = tmp_path / "run"

    ResearchWorkflow(MockAdapter()).run(
        request,
        loaded,
        run_dir,
        evidence_artifact_dir=source_dir,
    )

    assert (run_dir / "raw" / "quote-response.json").read_bytes() == raw
    reloaded = load_evidence(run_dir / "eligible_evidence.json")
    assert [item.evidence_id for item in reloaded] == ["quote-1", "quote-2"]
    bundle_data = json.loads((run_dir / "eligible_evidence.json").read_text())
    assert list(bundle_data) == ["evidence"]
