from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value


def _require_optional_timezone(value: datetime | None) -> datetime | None:
    if value is not None:
        _require_timezone(value)
    return value


class ResearchRequest(StrictModel):
    request_id: str = Field(min_length=1)
    question: str = Field(min_length=8)
    universe: list[str] = Field(min_length=1)
    as_of: datetime
    horizon: str = Field(min_length=1)
    allowed_evidence_ids: list[str] = Field(min_length=1)

    _as_of_timezone = field_validator("as_of")(_require_timezone)


class EvidenceProvenance(StrictModel):
    provider: str = Field(min_length=1)
    source_type: Literal["market_quote_snapshot"]
    source_endpoint: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    observed_at: datetime
    source_event_at: datetime | None = None
    freshness: Literal["real_time", "delayed", "end_of_day", "unknown"] = "unknown"
    raw_artifact_ref: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalizer_version: str = Field(min_length=1)

    _observed_timezone = field_validator("observed_at")(_require_timezone)
    _source_event_timezone = field_validator("source_event_at")(
        _require_optional_timezone
    )

    @model_validator(mode="after")
    def validate_source_timeline(self) -> EvidenceProvenance:
        if self.source_event_at is not None and self.source_event_at > self.observed_at:
            raise ValueError("source_event_at must not be after observed_at")
        return self


class Evidence(StrictModel):
    evidence_type: Literal["document", "market_quote_snapshot"] = "document"
    evidence_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    uri: HttpUrl
    locator: str = Field(min_length=1)
    excerpt: str = Field(min_length=8)
    published_at: datetime | None
    known_at: datetime
    retrieved_at: datetime
    available_at: datetime | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: EvidenceProvenance | None = None

    _published_timezone = field_validator("published_at")(_require_optional_timezone)
    _known_timezone = field_validator("known_at")(_require_timezone)
    _retrieved_timezone = field_validator("retrieved_at")(_require_timezone)
    _available_timezone = field_validator("available_at")(_require_optional_timezone)

    @field_validator("uri")
    @classmethod
    def reject_sensitive_url_components(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query:
            raise ValueError("evidence uri must not contain userinfo or query parameters")
        return value

    @model_validator(mode="after")
    def validate_timeline(self) -> Evidence:
        if self.evidence_type == "document" and self.published_at is None:
            raise ValueError("document evidence requires published_at")
        if self.evidence_type == "market_quote_snapshot":
            if self.provenance is None:
                raise ValueError("market quote evidence requires provenance")
            if self.published_at is not None:
                raise ValueError("market quote snapshot published_at must be null")
            if self.available_at is None:
                raise ValueError("market quote evidence requires available_at")
        if self.provenance is not None and self.provenance.source_type != self.evidence_type:
            raise ValueError("provenance source_type must match evidence_type")
        if self.published_at is not None and self.published_at > self.known_at:
            raise ValueError("published_at must not be after known_at")
        if self.known_at > self.retrieved_at:
            raise ValueError("known_at must not be after retrieved_at")
        if self.available_at is not None and self.retrieved_at > self.available_at:
            raise ValueError("retrieved_at must not be after available_at")
        if self.provenance is not None and self.provenance.observed_at > self.retrieved_at:
            raise ValueError("provenance observed_at must not be after retrieved_at")
        return self


ClaimKind = Literal["fact", "estimate", "forecast", "opinion"]
ClaimStatus = Literal["unverified", "verified", "disputed", "insufficient"]


class Claim(StrictModel):
    claim_id: str = Field(min_length=1)
    kind: ClaimKind
    text: str = Field(min_length=8)
    evidence_ids: list[str] = Field(min_length=1)
    as_of: datetime
    confidence: float = Field(ge=0, le=1)
    status: ClaimStatus
    verifier_notes: str = ""
    assumptions: list[str] = Field(default_factory=list)

    _as_of_timezone = field_validator("as_of")(_require_timezone)

    @model_validator(mode="after")
    def validate_forecast(self) -> Claim:
        if self.kind == "forecast" and not self.assumptions:
            raise ValueError("forecast claims require assumptions")
        return self


class ClaimDraft(StrictModel):
    claim_id: str = Field(min_length=1)
    kind: ClaimKind
    text: str = Field(min_length=8)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_forecast(self) -> ClaimDraft:
        if self.kind == "forecast" and not self.assumptions:
            raise ValueError("forecast claims require assumptions")
        return self


class ClaimDraftBatch(StrictModel):
    claims: list[ClaimDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_claim_ids(self) -> ClaimDraftBatch:
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim_id values must be unique")
        return self


class ClaimVerification(StrictModel):
    claim_id: str = Field(min_length=1)
    status: Literal["verified", "disputed", "insufficient"]
    verifier_notes: str = Field(min_length=1)


class VerificationBatch(StrictModel):
    verifications: list[ClaimVerification] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_claim_ids(self) -> VerificationBatch:
        ids = [item.claim_id for item in self.verifications]
        if len(ids) != len(set(ids)):
            raise ValueError("verification claim_id values must be unique")
        return self


class ClaimBatch(StrictModel):
    claims: list[Claim] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_claim_ids(self) -> ClaimBatch:
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim_id values must be unique")
        return self


class GroundedStatement(StrictModel):
    text: str = Field(min_length=8)
    claim_ids: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    trust_status: Literal["UNVERIFIED_NARRATIVE"] = "UNVERIFIED_NARRATIVE"


class ChallengeMemo(StrictModel):
    counter_thesis: GroundedStatement
    challenged_claim_ids: list[str] = Field(default_factory=list)
    risks: list[GroundedStatement] = Field(min_length=1)
    missing_information: list[str] = Field(min_length=1)


class ReportDraft(StrictModel):
    executive_summary: GroundedStatement
    thesis: GroundedStatement
    risks: list[GroundedStatement] = Field(min_length=1)
    missing_information: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    monitoring_items: list[GroundedStatement] = Field(min_length=1)
    invalidation_conditions: list[GroundedStatement] = Field(min_length=1)


class ResearchReport(StrictModel):
    run_id: str
    request_id: str
    question: str
    as_of: datetime
    executive_summary: GroundedStatement
    thesis: GroundedStatement
    dissent: GroundedStatement
    verified_claims: list[Claim]
    disputed_claims: list[Claim]
    insufficient_claims: list[Claim]
    risks: list[GroundedStatement]
    missing_information: list[str]
    limitations: list[str]
    monitoring_items: list[GroundedStatement]
    invalidation_conditions: list[GroundedStatement]
    evidence_ids: list[str]
    rejected_evidence_ids: list[str]
    provider: str
    model: str
    prompt_version: str
    stage: Literal["HUMAN_REVIEW_REQUIRED"] = "HUMAN_REVIEW_REQUIRED"
    human_review_required: Literal[True] = True
    narrative_requires_human_verification: Literal[True] = True

    _as_of_timezone = field_validator("as_of")(_require_timezone)


class EvidenceBundle(StrictModel):
    evidence: list[Evidence] = Field(min_length=1)


class RejectedEvidence(StrictModel):
    evidence_id: str
    reason: str
