from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from finresearch.schemas import Claim, Evidence, EvidenceProvenance


def test_evidence_rejects_impossible_timeline() -> None:
    known_at = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="ev-1",
            title="Title",
            publisher="Publisher",
            uri="https://example.com/item",
            locator="p. 1",
            excerpt="A sufficiently long excerpt.",
            published_at=known_at + timedelta(days=1),
            known_at=known_at,
            retrieved_at=known_at + timedelta(days=2),
            content_sha256="a" * 64,
            record_sha256="b" * 64,
        )


def test_forecast_requires_assumptions() -> None:
    with pytest.raises(ValidationError):
        Claim(
            claim_id="claim-1",
            kind="forecast",
            text="Revenue may grow next year.",
            evidence_ids=["ev-1"],
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            confidence=0.5,
            status="unverified",
        )


def test_evidence_is_immutable() -> None:
    known_at = datetime(2026, 1, 1, tzinfo=UTC)
    evidence = Evidence(
        evidence_id="ev-1",
        title="Title",
        publisher="Publisher",
        uri="https://example.com/item",
        locator="p. 1",
        excerpt="A sufficiently long excerpt.",
        published_at=known_at,
        known_at=known_at,
        retrieved_at=known_at + timedelta(days=1),
        content_sha256="a" * 64,
        record_sha256="b" * 64,
    )

    with pytest.raises(ValidationError, match="frozen"):
        evidence.known_at = known_at - timedelta(days=1)


def test_evidence_uri_rejects_query_tokens() -> None:
    known_at = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="userinfo or query"):
        Evidence(
            evidence_id="ev-1",
            title="Title",
            publisher="Publisher",
            uri="https://example.com/item?token=secret",
            locator="p. 1",
            excerpt="A sufficiently long excerpt.",
            published_at=known_at,
            known_at=known_at,
            retrieved_at=known_at + timedelta(days=1),
            content_sha256="a" * 64,
            record_sha256="b" * 64,
        )


def test_market_quote_requires_provenance_but_not_document_published_at() -> None:
    observed_at = datetime(2026, 8, 31, tzinfo=UTC)
    provenance = EvidenceProvenance(
        provider="longbridge",
        source_type="market_quote_snapshot",
        source_endpoint="cli.quote",
        symbol="NVDA.US",
        observed_at=observed_at,
        freshness="unknown",
        raw_artifact_ref="raw-response.json",
        raw_sha256="a" * 64,
        normalized_sha256="b" * 64,
        normalizer_version="longbridge.quote.v1",
    )
    market = Evidence(
        evidence_type="market_quote_snapshot",
        evidence_id="lbq-nvda",
        title="Longbridge quote snapshot",
        publisher="Longbridge Securities",
        uri="https://open.longbridge.com",
        locator="cli.quote NVDA.US",
        excerpt="A sufficiently long quote snapshot.",
        published_at=None,
        known_at=observed_at,
        retrieved_at=observed_at,
        available_at=observed_at + timedelta(milliseconds=1),
        content_sha256="c" * 64,
        record_sha256="d" * 64,
        provenance=provenance,
    )

    assert market.published_at is None
    with pytest.raises(ValidationError, match="published_at must be null"):
        Evidence(
            **{
                **market.model_dump(mode="json"),
                "published_at": observed_at,
            }
        )
    with pytest.raises(ValidationError, match="requires available_at"):
        Evidence(
            **{
                **market.model_dump(mode="json"),
                "available_at": None,
            }
        )
    with pytest.raises(ValidationError, match="document evidence requires published_at"):
        Evidence(
            evidence_id="doc-without-published-at",
            title="Document",
            publisher="Publisher",
            uri="https://example.com/document",
            locator="p. 1",
            excerpt="A sufficiently long document excerpt.",
            published_at=None,
            known_at=observed_at,
            retrieved_at=observed_at,
            content_sha256="e" * 64,
            record_sha256="f" * 64,
        )
