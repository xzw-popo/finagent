from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from finresearch.audit import AuditTrail
from finresearch.evidence import filter_evidence_as_of, jsonable, validate_evidence_records
from finresearch.llm.base import LLMAdapter
from finresearch.policy import NoTradePolicy
from finresearch.prompts import PROMPT_VERSION
from finresearch.schemas import (
    ChallengeMemo,
    Claim,
    ClaimBatch,
    ClaimDraft,
    ClaimDraftBatch,
    Evidence,
    GroundedStatement,
    ReportDraft,
    ResearchReport,
    ResearchRequest,
    VerificationBatch,
)


class Stage(StrEnum):
    VALIDATE_REQUEST = "VALIDATE_REQUEST"
    LOAD_AND_FILTER_EVIDENCE = "LOAD_AND_FILTER_EVIDENCE"
    EXTRACT_CLAIMS = "EXTRACT_CLAIMS"
    VERIFY_CLAIMS = "VERIFY_CLAIMS"
    CHALLENGE_CLAIMS = "CHALLENGE_CLAIMS"
    SYNTHESIZE_REPORT = "SYNTHESIZE_REPORT"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


ALLOWED_TRANSITIONS = {
    Stage.VALIDATE_REQUEST: Stage.LOAD_AND_FILTER_EVIDENCE,
    Stage.LOAD_AND_FILTER_EVIDENCE: Stage.EXTRACT_CLAIMS,
    Stage.EXTRACT_CLAIMS: Stage.VERIFY_CLAIMS,
    Stage.VERIFY_CLAIMS: Stage.CHALLENGE_CLAIMS,
    Stage.CHALLENGE_CLAIMS: Stage.SYNTHESIZE_REPORT,
    Stage.SYNTHESIZE_REPORT: Stage.HUMAN_REVIEW_REQUIRED,
}


class WorkflowStateMachine:
    def __init__(self) -> None:
        self.stage = Stage.VALIDATE_REQUEST

    def transition(self, target: Stage) -> None:
        expected = ALLOWED_TRANSITIONS.get(self.stage)
        if target != expected:
            raise RuntimeError(f"illegal workflow transition: {self.stage} -> {target}")
        self.stage = target


def _dump(value: Any) -> Any:
    return jsonable(value)


def _validate_draft_claims(claims: list[ClaimDraft], evidence_ids: set[str]) -> None:
    for claim in claims:
        unknown = set(claim.evidence_ids) - evidence_ids
        if unknown:
            raise ValueError(
                f"claim {claim.claim_id} references unknown evidence: {sorted(unknown)}"
            )


def _validate_claims(claims: list[Claim], request: ResearchRequest, evidence_ids: set[str]) -> None:
    for claim in claims:
        unknown = set(claim.evidence_ids) - evidence_ids
        if unknown:
            raise ValueError(
                f"claim {claim.claim_id} references unknown evidence: {sorted(unknown)}"
            )
        if claim.as_of != request.as_of:
            raise ValueError(f"claim {claim.claim_id} has an incorrect as_of timestamp")
        if claim.status == "unverified":
            raise ValueError(f"claim {claim.claim_id} remained unverified")


def _validate_same_claim_set(before: list[ClaimDraft], after: VerificationBatch) -> None:
    before_ids = {claim.claim_id for claim in before}
    after_ids = {item.claim_id for item in after.verifications}
    if before_ids != after_ids:
        raise ValueError("verifier must preserve the complete claim_id set")


def _validate_grounding(
    statements: GroundedStatement | list[GroundedStatement],
    claims_by_id: dict[str, Claim],
    label: str,
    allowed_statuses: set[str] | None = None,
) -> None:
    items = statements if isinstance(statements, list) else [statements]
    for item in items:
        unknown = set(item.claim_ids) - set(claims_by_id)
        if unknown:
            raise ValueError(f"{label} references unknown claims: {sorted(unknown)}")
        if allowed_statuses is not None:
            invalid = {
                claim_id: claims_by_id[claim_id].status
                for claim_id in item.claim_ids
                if claims_by_id[claim_id].status not in allowed_statuses
            }
            if invalid:
                raise ValueError(f"{label} references claims with disallowed status: {invalid}")


class ResearchWorkflow:
    def __init__(self, llm: LLMAdapter, policy: NoTradePolicy | None = None) -> None:
        self.llm = llm
        self.policy = policy or NoTradePolicy()

    def run(
        self,
        request: ResearchRequest,
        evidence: list[Evidence],
        output_dir: Path,
    ) -> ResearchReport:
        machine = WorkflowStateMachine()
        audit = AuditTrail()
        run_id = str(uuid4())
        audit.record(machine.stage, _dump(request))

        self.policy.authorize("read_local_evidence")
        machine.transition(Stage.LOAD_AND_FILTER_EVIDENCE)
        evidence = validate_evidence_records(evidence)
        eligible, rejected = filter_evidence_as_of(request, evidence)
        evidence_ids = {item.evidence_id for item in eligible}
        model_request = _dump(request)
        model_request.pop("allowed_evidence_ids", None)
        audit.record(
            machine.stage,
            _dump(eligible),
            {"eligible_count": len(eligible), "rejected_count": len(rejected)},
        )

        self.policy.authorize("call_json_llm")
        machine.transition(Stage.EXTRACT_CLAIMS)
        extracted = self.llm.generate(
            "extract",
            {"request": model_request, "evidence": _dump(eligible)},
            ClaimDraftBatch,
        )
        _validate_draft_claims(extracted.claims, evidence_ids)
        audit.record(machine.stage, _dump(extracted))

        machine.transition(Stage.VERIFY_CLAIMS)
        verification = self.llm.generate(
            "verify",
            {
                "request": model_request,
                "evidence": _dump(eligible),
                "claims": _dump(extracted.claims),
            },
            VerificationBatch,
        )
        _validate_same_claim_set(extracted.claims, verification)
        verification_by_id = {item.claim_id: item for item in verification.verifications}
        verified = ClaimBatch(
            claims=[
                Claim(
                    **draft.model_dump(),
                    as_of=request.as_of,
                    status=verification_by_id[draft.claim_id].status,
                    verifier_notes=verification_by_id[draft.claim_id].verifier_notes,
                )
                for draft in extracted.claims
            ]
        )
        _validate_claims(verified.claims, request, evidence_ids)
        audit.record(machine.stage, _dump(verified))

        machine.transition(Stage.CHALLENGE_CLAIMS)
        challenge = self.llm.generate(
            "challenge",
            {
                "request": model_request,
                "evidence": _dump(eligible),
                "claims": _dump(verified.claims),
            },
            ChallengeMemo,
        )
        claim_ids = {claim.claim_id for claim in verified.claims}
        claims_by_id = {claim.claim_id: claim for claim in verified.claims}
        unknown_challenges = set(challenge.challenged_claim_ids) - claim_ids
        if unknown_challenges:
            raise ValueError(
                f"challenge references unknown claims: {sorted(unknown_challenges)}"
            )
        _validate_grounding(challenge.counter_thesis, claims_by_id, "counter thesis")
        _validate_grounding(challenge.risks, claims_by_id, "challenge risk")
        audit.record(machine.stage, _dump(challenge))

        machine.transition(Stage.SYNTHESIZE_REPORT)
        draft = self.llm.generate(
            "synthesize",
            {
                "request": model_request,
                "claims": _dump(verified.claims),
                "challenge": _dump(challenge),
            },
            ReportDraft,
        )
        trusted_statuses = {"verified"}
        _validate_grounding(
            draft.executive_summary,
            claims_by_id,
            "executive summary",
            trusted_statuses,
        )
        _validate_grounding(draft.thesis, claims_by_id, "thesis", trusted_statuses)
        _validate_grounding(draft.risks, claims_by_id, "report risk", trusted_statuses)
        _validate_grounding(
            draft.monitoring_items,
            claims_by_id,
            "monitoring item",
            trusted_statuses,
        )
        _validate_grounding(
            draft.invalidation_conditions,
            claims_by_id,
            "invalidation condition",
            trusted_statuses,
        )
        report = ResearchReport(
            run_id=run_id,
            request_id=request.request_id,
            question=request.question,
            as_of=request.as_of,
            executive_summary=draft.executive_summary,
            thesis=draft.thesis,
            dissent=challenge.counter_thesis,
            verified_claims=[claim for claim in verified.claims if claim.status == "verified"],
            disputed_claims=[claim for claim in verified.claims if claim.status == "disputed"],
            insufficient_claims=[
                claim for claim in verified.claims if claim.status == "insufficient"
            ],
            risks=draft.risks,
            missing_information=draft.missing_information,
            limitations=draft.limitations,
            monitoring_items=draft.monitoring_items,
            invalidation_conditions=draft.invalidation_conditions,
            evidence_ids=sorted(evidence_ids),
            rejected_evidence_ids=sorted(item.evidence_id for item in rejected),
            provider=self.llm.provider,
            model=self.llm.model,
            prompt_version=PROMPT_VERSION,
        )
        audit.record(machine.stage, _dump(report))

        machine.transition(Stage.HUMAN_REVIEW_REQUIRED)
        audit.record(machine.stage, {"run_id": run_id}, {"human_review_required": True})

        self.policy.authorize("write_local_report")
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = {
            "request.json": request,
            "eligible_evidence.json": eligible,
            "rejected_evidence.json": rejected,
            "claims.json": verified,
            "challenge.json": challenge,
            "report.json": report,
        }
        for filename, value in artifacts.items():
            (output_dir / filename).write_text(
                json.dumps(_dump(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        audit.write(output_dir / "events.jsonl")
        return report
