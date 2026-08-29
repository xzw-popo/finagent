import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finresearch.evidence import load_evidence, load_request
from finresearch.llm.mock import MockAdapter
from finresearch.schemas import Claim, GroundedStatement
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


def test_state_machine_rejects_skipped_gate() -> None:
    machine = WorkflowStateMachine()
    with pytest.raises(RuntimeError, match="illegal workflow transition"):
        machine.transition(Stage.EXTRACT_CLAIMS)


def test_mock_workflow_is_auditable_and_requires_human_review(tmp_path: Path) -> None:
    request = load_request(ROOT / "examples/request.json")
    evidence = load_evidence(ROOT / "examples/evidence.json")

    adapter = RecordingMockAdapter()
    report = ResearchWorkflow(adapter).run(request, evidence, tmp_path)

    assert report.stage == "HUMAN_REVIEW_REQUIRED"
    assert report.human_review_required is True
    assert report.narrative_requires_human_verification is True
    assert report.executive_summary.trust_status == "UNVERIFIED_NARRATIVE"
    assert report.rejected_evidence_ids == ["ev-future-guidance"]
    assert "ev-future-guidance" not in report.evidence_ids
    assert len(report.verified_claims) == 2
    assert all("ev-future-guidance" not in json.dumps(payload) for payload in adapter.payloads)
    assert (tmp_path / "report.json").exists()

    event_lines = (tmp_path / "events.jsonl").read_text().splitlines()
    stages = [json.loads(line)["stage"] for line in event_lines]
    assert stages == [stage.value for stage in Stage]


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
