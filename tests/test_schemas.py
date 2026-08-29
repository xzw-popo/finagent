from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from finresearch.schemas import Claim, Evidence


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
