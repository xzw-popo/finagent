from __future__ import annotations

import hashlib
import json
from pathlib import Path

from finresearch.schemas import Evidence, EvidenceBundle, RejectedEvidence, ResearchRequest


def load_request(path: Path) -> ResearchRequest:
    return ResearchRequest.model_validate_json(path.read_text(encoding="utf-8"))


def evidence_record_sha256(item: Evidence) -> str:
    payload = item.model_dump(mode="json", exclude={"record_sha256"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_evidence_records(evidence: list[Evidence]) -> list[Evidence]:
    validated = [Evidence.model_validate(item.model_dump()) for item in evidence]
    ids = [item.evidence_id for item in validated]
    if len(ids) != len(set(ids)):
        raise ValueError("evidence_id values must be unique")
    for item in validated:
        digest = hashlib.sha256(item.excerpt.encode("utf-8")).hexdigest()
        if digest != item.content_sha256:
            raise ValueError(f"content_sha256 mismatch for evidence {item.evidence_id}")
        if evidence_record_sha256(item) != item.record_sha256:
            raise ValueError(f"record_sha256 mismatch for evidence {item.evidence_id}")
    return validated


def load_evidence(path: Path) -> list[Evidence]:
    bundle = EvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
    return validate_evidence_records(bundle.evidence)


def filter_evidence_as_of(
    request: ResearchRequest, evidence: list[Evidence]
) -> tuple[list[Evidence], list[RejectedEvidence]]:
    allowed = set(request.allowed_evidence_ids)
    eligible: list[Evidence] = []
    rejected: list[RejectedEvidence] = []
    for item in evidence:
        if allowed and item.evidence_id not in allowed:
            rejected.append(
                RejectedEvidence(evidence_id=item.evidence_id, reason="not in request allowlist")
            )
        elif item.known_at > request.as_of:
            rejected.append(
                RejectedEvidence(evidence_id=item.evidence_id, reason="known_at is after as_of")
            )
        else:
            eligible.append(item)
    if not eligible:
        raise ValueError("no eligible evidence remains after allowlist and as-of filtering")
    return eligible, rejected


def jsonable(items: object) -> object:
    if hasattr(items, "model_dump"):
        return items.model_dump(mode="json")
    if isinstance(items, list):
        return [jsonable(item) for item in items]
    return json.loads(json.dumps(items, default=str))
