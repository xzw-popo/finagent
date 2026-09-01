from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from finresearch.evidence import evidence_record_sha256, validate_evidence_records
from finresearch.evidence_merge import (
    DEFAULT_MERGE_LIMITS,
    EvidenceMergeError,
    _create_directory_at,
    _create_private_staging_at,
    _directory_entry_matches_fd,
    _directory_path_matches_fd,
    _entry_exists_at,
    _hash_file_at,
    _open_directory_at,
    _open_output_parent,
    _read_file_at,
    _rename_directory_noreplace_at,
    _require_safe_filesystem_capabilities,
    _write_file_at_fsynced,
)
from finresearch.marketdata.longbridge import (
    LongbridgeCollectionError,
    LongbridgeCollectorConfig,
    LongbridgeQuoteCollector,
    ProviderWarningMetadata,
    _canonical_json_bytes,
    _decode_json,
    _ensure_utc,
    _iso_utc,
    normalize_symbols,
)
from finresearch.schemas import Evidence, EvidenceBundle, EvidenceProvenance, StrictModel

FinancialReport = Literal["af", "saf", "qf"]
FinancialStatementKind = Literal["IS", "BS", "CF"]

FINANCIAL_REPORTS = frozenset({"af", "saf", "qf"})
STATEMENT_KINDS: tuple[FinancialStatementKind, ...] = ("IS", "BS", "CF")
FINANCIAL_STATEMENT_NORMALIZER_VERSION = "longbridge.financial_statement.v1"
BUSINESS_SEGMENT_NORMALIZER_VERSION = "longbridge.business_segments.v1"
FINANCIAL_SOURCE_URI = "https://open.longbridge.com/"
_MAX_TEXT_LENGTH = 4_096
_MAX_FINANCIAL_EVIDENCE_BUNDLE_BYTES = DEFAULT_MERGE_LIMITS.max_bundle_bytes


@dataclass(frozen=True)
class FinancialsCollectorConfig:
    timeout_seconds: float = 20.0
    max_stdout_bytes: int = 4 * 1024 * 1024
    max_stderr_bytes: int = 65_536
    region: str = "auto"

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("Longbridge timeout must be between 0 and 120 seconds")
        if self.max_stdout_bytes < 1 or self.max_stderr_bytes < 1:
            raise ValueError("Longbridge output limits must be positive")
        if self.region not in {"auto", "cn", "global"}:
            raise ValueError("Longbridge region must be one of: auto, cn, global")


class NormalizedFinancialField(StrictModel):
    name: str
    field_id: str
    level: int
    display_order: int
    value: str | None
    yoy_ratio: str | None
    field: str | None
    value_type: str


class NormalizedFinancialPeriod(StrictModel):
    fiscal_year: str
    fiscal_period: str
    report_label: str
    fiscal_period_end: str | None
    report_date: str | None
    fields: tuple[NormalizedFinancialField, ...]


class NormalizedFinancialStatement(StrictModel):
    statement_kind: FinancialStatementKind
    report: FinancialReport
    currency: str
    periods: tuple[NormalizedFinancialPeriod, ...]
    empty_field_ids: tuple[str, ...]


class NormalizedBusinessSegment(StrictModel):
    name: str
    segment_id: str
    value: str | None
    percent: str | None
    yoy_percent: str | None


class NormalizedBusinessSegmentPeriod(StrictModel):
    report: str
    report_label: str | None
    currency: str
    total: str | None
    date: str | None
    fiscal_period_start: str | None
    fiscal_period_end: str | None
    report_date: str | None
    yoy_percent: str | None
    business: tuple[NormalizedBusinessSegment, ...]
    regionals: tuple[NormalizedBusinessSegment, ...]
    business_ids: tuple[str, ...]
    regional_ids: tuple[str, ...]


class NormalizedBusinessSegments(StrictModel):
    report: Literal["af", "saf", "qf"]
    periods: tuple[NormalizedBusinessSegmentPeriod, ...]
    business_ids: tuple[str, ...]
    regional_ids: tuple[str, ...]


@dataclass(frozen=True)
class FinancialRawArtifact:
    source: str
    path: str
    payload: bytes
    sha256: str
    requested_at: datetime
    retrieved_at: datetime

    def manifest(self) -> dict[str, Any]:
        duration_ms = int((self.retrieved_at - self.requested_at).total_seconds() * 1000)
        return {
            "source": self.source,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": len(self.payload),
            "requested_at": _iso_utc(self.requested_at),
            "retrieved_at": _iso_utc(self.retrieved_at),
            "duration_ms": duration_ms,
        }


@dataclass(frozen=True)
class FinancialCollection:
    symbol: str
    report: FinancialReport
    include_segments: bool
    statements: tuple[NormalizedFinancialStatement, ...]
    segments: NormalizedBusinessSegments | None
    artifacts: tuple[FinancialRawArtifact, ...]
    evidence: tuple[Evidence, ...]
    requested_at: datetime
    retrieved_at: datetime
    available_at: datetime
    cli_version: str
    warnings: tuple[ProviderWarningMetadata, ...]

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": "finresearch.longbridge_financial_collection.v1",
            "provider": "longbridge",
            "provider_cli_version": self.cli_version,
            "symbol": self.symbol,
            "report": self.report,
            "required_statement_kinds": list(STATEMENT_KINDS),
            "collected_statement_kinds": [item.statement_kind for item in self.statements],
            "include_segments": self.include_segments,
            "requested_at": _iso_utc(self.requested_at),
            "retrieved_at": _iso_utc(self.retrieved_at),
            "available_at": _iso_utc(self.available_at),
            "raw_responses": [artifact.manifest() for artifact in self.artifacts],
            "evidence": {
                "path": "evidence.json",
                "evidence_ids": [item.evidence_id for item in self.evidence],
            },
            "time_semantics": {
                "financial_period_dates": "descriptive_only_not_pit_availability",
                "source_event_at": "unavailable_from_financial_statement_commands",
                "known_at": "collector_received_each_complete_response",
                "available_at": "all_requested_responses_normalized",
            },
            "pit_filter_applied": False,
            "warnings": [warning.manifest() for warning in self.warnings],
        }


@dataclass(frozen=True)
class FinancialCollectionWriteResult:
    evidence_path: Path
    manifest_path: Path
    durability_confirmed: bool = True


def _required_text(value: Any, field: str, *, max_length: int = _MAX_TEXT_LENGTH) -> str:
    normalized = _optional_text(value, field, max_length=max_length)
    if normalized is None:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} is required"
        )
    return normalized


def _optional_text(
    value: Any, field: str, *, max_length: int = _MAX_TEXT_LENGTH
) -> str | None:
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, (bool, dict, list, tuple)):
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} must be scalar"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LongbridgeCollectionError(
                "schema_mismatch", f"Longbridge financial field {field!r} must be finite"
            )
        decimal_tuple = value.as_tuple()
        if len(decimal_tuple.digits) > 100 or not -100 <= decimal_tuple.exponent <= 100:
            raise LongbridgeCollectionError(
                "schema_mismatch",
                f"Longbridge financial field {field!r} exceeds numeric limits",
            )
        text = format(value, "f")
    elif isinstance(value, (str, int)):
        text = str(value).strip()
    else:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} must be scalar"
        )
    if not text:
        return None
    if len(text) > max_length:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} is too long"
        )
    return text


def _required_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} must be an integer"
        )
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        stripped = value.strip()
        if len(stripped) > 16:
            raise LongbridgeCollectionError(
                "schema_mismatch", f"Longbridge financial field {field!r} is out of range"
            )
        try:
            result = int(stripped)
        except (ValueError, OverflowError):
            raise LongbridgeCollectionError(
                "schema_mismatch",
                f"Longbridge financial field {field!r} must be an integer",
            ) from None
    else:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} must be an integer"
        )
    if not -1_000_000 <= result <= 1_000_000:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} is out of range"
        )
    return result


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge financial field {field!r} must be an array"
        )
    result = tuple(
        _required_text(item, f"{field}[]", max_length=256) for item in value
    )
    if len(result) != len(set(result)):
        raise LongbridgeCollectionError(
            "protocol_mismatch", f"Longbridge financial field {field!r} has duplicates"
        )
    return result


def _validate_statement_envelope(payload: Any, expected_report: str) -> list[Any]:
    if not isinstance(payload, dict):
        raise LongbridgeCollectionError(
            "schema_mismatch", "Longbridge financial statement response must be an object"
        )
    report = _required_text(payload.get("report"), "report", max_length=16)
    if report != expected_report:
        raise LongbridgeCollectionError(
            "protocol_mismatch",
            "Longbridge financial statement response report does not match the request",
        )
    periods = payload.get("list")
    if not isinstance(periods, list):
        raise LongbridgeCollectionError(
            "schema_mismatch", "Longbridge financial statement list must be an array"
        )
    return periods


def _normalize_statement(
    payload: Any,
    *,
    statement_kind: FinancialStatementKind,
    expected_report: FinancialReport,
) -> NormalizedFinancialStatement:
    periods_raw = _validate_statement_envelope(payload, expected_report)
    if not periods_raw:
        raise LongbridgeCollectionError(
            "no_data", f"Longbridge returned no {statement_kind} financial statement data"
        )
    currency = _required_text(payload.get("currency"), "currency", max_length=16)
    periods: list[NormalizedFinancialPeriod] = []
    period_keys: set[tuple[str, str, str | None]] = set()
    for period_index, raw_period in enumerate(periods_raw):
        if not isinstance(raw_period, dict):
            raise LongbridgeCollectionError(
                "schema_mismatch", "each Longbridge financial period must be an object"
            )
        fiscal_year = _required_text(
            raw_period.get("ff_year"), f"list[{period_index}].ff_year", max_length=16
        )
        fiscal_period = _required_text(
            raw_period.get("ff_period"), f"list[{period_index}].ff_period", max_length=16
        )
        fiscal_period_end = _optional_text(
            raw_period.get("fp_end"), f"list[{period_index}].fp_end", max_length=32
        )
        period_key = (fiscal_year, fiscal_period, fiscal_period_end)
        if period_key in period_keys:
            raise LongbridgeCollectionError(
                "protocol_mismatch", "Longbridge returned a duplicate financial period"
            )
        period_keys.add(period_key)
        raw_fields = raw_period.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            raise LongbridgeCollectionError(
                "schema_mismatch", "each Longbridge financial period requires fields"
            )
        fields: list[NormalizedFinancialField] = []
        field_ids: set[str] = set()
        for field_index, raw_field in enumerate(raw_fields):
            if not isinstance(raw_field, dict):
                raise LongbridgeCollectionError(
                    "schema_mismatch", "each Longbridge financial field must be an object"
                )
            prefix = f"list[{period_index}].fields[{field_index}]"
            field_id = _required_text(raw_field.get("id"), f"{prefix}.id", max_length=128)
            if field_id in field_ids:
                raise LongbridgeCollectionError(
                    "protocol_mismatch", "Longbridge returned duplicate financial field ids"
                )
            field_ids.add(field_id)
            fields.append(
                NormalizedFinancialField(
                    name=_required_text(raw_field.get("name"), f"{prefix}.name"),
                    field_id=field_id,
                    level=_required_integer(raw_field.get("level"), f"{prefix}.level"),
                    display_order=_required_integer(
                        raw_field.get("display_order"), f"{prefix}.display_order"
                    ),
                    value=_optional_text(raw_field.get("value"), f"{prefix}.value"),
                    yoy_ratio=_optional_text(raw_field.get("yoy"), f"{prefix}.yoy"),
                    field=_optional_text(
                        raw_field.get("field"), f"{prefix}.field", max_length=256
                    ),
                    value_type=_required_text(
                        raw_field.get("value_type"), f"{prefix}.value_type", max_length=128
                    ),
                )
            )
        periods.append(
            NormalizedFinancialPeriod(
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                report_label=_required_text(
                    raw_period.get("report_txt"),
                    f"list[{period_index}].report_txt",
                    max_length=256,
                ),
                fiscal_period_end=fiscal_period_end,
                report_date=_optional_text(
                    raw_period.get("rpt_date"),
                    f"list[{period_index}].rpt_date",
                    max_length=32,
                ),
                fields=tuple(fields),
            )
        )
    return NormalizedFinancialStatement(
        statement_kind=statement_kind,
        report=expected_report,
        currency=currency,
        periods=tuple(periods),
        empty_field_ids=_string_list(payload.get("empty_fields", []), "empty_fields"),
    )


def _validate_statement_set(
    statements: tuple[NormalizedFinancialStatement, ...],
) -> None:
    if tuple(item.statement_kind for item in statements) != STATEMENT_KINDS:
        raise LongbridgeCollectionError(
            "partial_result", "Longbridge did not return exactly IS, BS and CF"
        )
    if any(not statement.periods for statement in statements):
        raise LongbridgeCollectionError(
            "partial_result", "Longbridge omitted one or more required financial statements"
        )
    currencies = {statement.currency for statement in statements}
    if len(currencies) != 1:
        raise LongbridgeCollectionError(
            "protocol_mismatch", "Longbridge financial statement currencies do not align"
        )
    latest_periods = {
        (
            statement.periods[0].fiscal_year,
            statement.periods[0].fiscal_period,
            statement.periods[0].fiscal_period_end,
        )
        for statement in statements
    }
    if len(latest_periods) != 1:
        raise LongbridgeCollectionError(
            "protocol_mismatch",
            "Longbridge financial statement latest periods do not align",
        )


def _normalize_segment_entries(value: Any, field: str) -> tuple[NormalizedBusinessSegment, ...]:
    if not isinstance(value, list):
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge business segment field {field!r} must be an array"
        )
    result: list[NormalizedBusinessSegment] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise LongbridgeCollectionError(
                "schema_mismatch", "each Longbridge business segment must be an object"
            )
        prefix = f"{field}[{index}]"
        segment_id = _required_text(raw.get("id"), f"{prefix}.id", max_length=128)
        if segment_id in seen:
            raise LongbridgeCollectionError(
                "protocol_mismatch", f"Longbridge returned duplicate {field} ids"
            )
        seen.add(segment_id)
        result.append(
            NormalizedBusinessSegment(
                name=_required_text(raw.get("name"), f"{prefix}.name"),
                segment_id=segment_id,
                value=_optional_text(raw.get("value"), f"{prefix}.value"),
                percent=_optional_text(raw.get("percent"), f"{prefix}.percent"),
                yoy_percent=_optional_text(raw.get("yoy"), f"{prefix}.yoy"),
            )
        )
    return tuple(result)


def _normalize_segment_period(payload: Any) -> NormalizedBusinessSegmentPeriod:
    if not isinstance(payload, dict):
        raise LongbridgeCollectionError(
            "schema_mismatch", "Longbridge business segments response must be an object"
        )
    business = _normalize_segment_entries(payload.get("business"), "business")
    regionals = _normalize_segment_entries(payload.get("regionals"), "regionals")
    if not business and not regionals:
        raise LongbridgeCollectionError(
            "partial_result", "Longbridge omitted data for a business segment period"
        )
    return NormalizedBusinessSegmentPeriod(
        report=_required_text(payload.get("report"), "report", max_length=16),
        report_label=_optional_text(payload.get("report_txt"), "report_txt", max_length=256),
        currency=_required_text(payload.get("currency"), "currency", max_length=16),
        total=_optional_text(payload.get("total"), "total"),
        date=_optional_text(payload.get("date"), "date", max_length=32),
        fiscal_period_start=_optional_text(payload.get("fp_start"), "fp_start", max_length=32),
        fiscal_period_end=_optional_text(payload.get("fp_end"), "fp_end", max_length=32),
        report_date=_optional_text(payload.get("rpt_date"), "rpt_date", max_length=32),
        yoy_percent=_optional_text(payload.get("yoy"), "yoy"),
        business=business,
        regionals=regionals,
        business_ids=_string_list(payload.get("bus_ids", []), "bus_ids"),
        regional_ids=_string_list(payload.get("reg_ids", []), "reg_ids"),
    )


def _normalize_segments(
    payload: Any, expected_report: FinancialReport
) -> NormalizedBusinessSegments:
    if not isinstance(payload, dict):
        raise LongbridgeCollectionError(
            "schema_mismatch", "Longbridge business segment history must be an object"
        )
    historical = payload.get("historical")
    if not isinstance(historical, list):
        raise LongbridgeCollectionError(
            "schema_mismatch", "Longbridge business segment historical must be an array"
        )
    if not historical:
        raise LongbridgeCollectionError(
            "partial_result", "Longbridge omitted the requested business segment history"
        )
    periods = tuple(_normalize_segment_period(item) for item in historical)
    if any(item.report != expected_report for item in periods):
        raise LongbridgeCollectionError(
            "protocol_mismatch",
            "Longbridge business segment report does not match the request",
        )
    period_keys = [(item.date, item.fiscal_period_end) for item in periods]
    if len(period_keys) != len(set(period_keys)):
        raise LongbridgeCollectionError(
            "protocol_mismatch", "Longbridge returned duplicate business segment periods"
        )
    return NormalizedBusinessSegments(
        report=expected_report,
        periods=periods,
        business_ids=_string_list(payload.get("bus_ids", []), "bus_ids"),
        regional_ids=_string_list(payload.get("reg_ids", []), "reg_ids"),
    )


def _build_snapshot_evidence(
    normalized: NormalizedFinancialStatement | NormalizedBusinessSegments,
    *,
    symbol: str,
    cli_version: str,
    retrieved_at: datetime,
    available_at: datetime,
    artifact: FinancialRawArtifact,
) -> Evidence:
    if isinstance(normalized, NormalizedFinancialStatement):
        evidence_type = "financial_statement_snapshot"
        endpoint = "cli.financial-statement"
        normalizer_version = FINANCIAL_STATEMENT_NORMALIZER_VERSION
        descriptor = f"{normalized.report}-{normalized.statement_kind.lower()}"
        title = f"长桥证券财务报表快照：{symbol} {normalized.statement_kind}"
        normalized_payload: dict[str, Any] = normalized.model_dump(mode="json")
    else:
        evidence_type = "business_segment_snapshot"
        endpoint = "cli.business-segments"
        normalizer_version = BUSINESS_SEGMENT_NORMALIZER_VERSION
        descriptor = "business-segments"
        title = f"长桥证券业务分部快照：{symbol}"
        normalized_payload = normalized.model_dump(mode="json")
    excerpt_payload = {
        "schema": normalizer_version,
        "provider": "longbridge",
        "symbol": symbol,
        "captured_at": _iso_utc(retrieved_at),
        "available_at": _iso_utc(available_at),
        "time_basis": (
            "collector_received_at; fiscal/report dates are descriptive and "
            "source_event_at is unavailable"
        ),
        "data": normalized_payload,
    }
    excerpt = _canonical_json_bytes(excerpt_payload).decode("utf-8")
    normalized_sha256 = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    slug = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")
    timestamp = available_at.strftime("%Y%m%dT%H%M%S%fZ")
    prefix = "lbf" if evidence_type == "financial_statement_snapshot" else "lbs"
    evidence_id = f"{prefix}-{slug}-{descriptor}-{timestamp}-{normalized_sha256[:12]}"
    provenance = EvidenceProvenance(
        provider="longbridge",
        source_type=evidence_type,
        source_endpoint=endpoint,
        symbol=symbol,
        observed_at=retrieved_at,
        source_event_at=None,
        freshness="unknown",
        raw_artifact_ref=artifact.path,
        raw_sha256=artifact.sha256,
        normalized_sha256=normalized_sha256,
        normalizer_version=normalizer_version,
    )
    locator = (
        f"provider=longbridge; endpoint={endpoint}; symbol={symbol}; "
        f"cli_version={cli_version}; observed_at={_iso_utc(retrieved_at)}; "
        f"source_event_at=unavailable; schema={normalizer_version}"
    )
    draft = Evidence(
        evidence_type=evidence_type,
        evidence_id=evidence_id,
        title=title,
        publisher="长桥证券",
        uri=FINANCIAL_SOURCE_URI,
        locator=locator,
        excerpt=excerpt,
        published_at=None,
        known_at=retrieved_at,
        retrieved_at=retrieved_at,
        available_at=available_at,
        content_sha256=normalized_sha256,
        record_sha256="0" * 64,
        provenance=provenance,
    )
    return Evidence.model_validate(
        {
            **draft.model_dump(mode="json"),
            "record_sha256": evidence_record_sha256(draft),
        }
    )


class LongbridgeFinancialsCollector:
    def __init__(
        self,
        config: FinancialsCollectorConfig | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config or FinancialsCollectorConfig()
        self._clock = clock
        self._runner = LongbridgeQuoteCollector(
            LongbridgeCollectorConfig(
                timeout_seconds=self.config.timeout_seconds,
                max_symbols=1,
                max_stdout_bytes=self.config.max_stdout_bytes,
                max_stderr_bytes=self.config.max_stderr_bytes,
                region=self.config.region,
            )
        )

    def _timestamp(self, label: str, previous: datetime | None = None) -> datetime:
        try:
            value = _ensure_utc(self._clock(), label)
        except LongbridgeCollectionError:
            raise
        except Exception as exc:
            raise LongbridgeCollectionError("clock_error", f"unable to read {label}") from exc
        if previous is not None and value < previous:
            raise LongbridgeCollectionError(
                "clock_error", f"{label} must not be before the previous collection time"
            )
        return value

    def _run_provider(self, argv: list[str], runtime_dir: str) -> Any:
        try:
            return self._runner._run(argv, runtime_dir)
        except LongbridgeCollectionError as exc:
            if exc.code != "command_failed":
                raise
            raise LongbridgeCollectionError(
                exc.code,
                "Longbridge financial data command failed",
                retryable=exc.retryable,
                exit_code=exc.exit_code,
            ) from None

    def collect(
        self,
        symbol: str,
        *,
        report: FinancialReport = "af",
        include_segments: bool = False,
    ) -> FinancialCollection:
        normalized_symbol = normalize_symbols([symbol], max_symbols=1)[0]
        if report not in FINANCIAL_REPORTS:
            raise LongbridgeCollectionError(
                "invalid_report",
                "complete financial bundles support only: af, saf, qf",
            )
        if not isinstance(include_segments, bool):
            raise LongbridgeCollectionError(
                "protocol_mismatch", "include_segments must be a boolean"
            )
        binary = self._runner._resolve_binary()
        completed: list[tuple[str, str, Any, FinancialRawArtifact]] = []
        warnings: list[ProviderWarningMetadata] = []
        previous: datetime | None = None
        with tempfile.TemporaryDirectory(
            prefix="finresearch-longbridge-financials-"
        ) as runtime_dir:
            version_result = self._run_provider(
                [str(binary), "--version"], runtime_dir
            )
            try:
                cli_version = version_result.stdout.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError:
                raise LongbridgeCollectionError(
                    "invalid_json", "Longbridge version output is not valid UTF-8"
                ) from None
            if re.fullmatch(r"longbridge [0-9A-Za-z.+-]+", cli_version) is None:
                raise LongbridgeCollectionError(
                    "schema_mismatch", "Longbridge CLI returned an invalid version string"
                )
            if version_result.stderr:
                warnings.append(
                    ProviderWarningMetadata(
                        source="cli.version.stderr",
                        sha256=hashlib.sha256(version_result.stderr).hexdigest(),
                        bytes=len(version_result.stderr),
                    )
                )

            for kind in STATEMENT_KINDS:
                command_source = f"cli.financial-statement.{kind}"
                requested_at = self._timestamp(f"{kind}.requested_at", previous)
                if previous is None:
                    collection_requested_at = requested_at
                result = self._run_provider(
                    [
                        str(binary),
                        "financial-statement",
                        normalized_symbol,
                        "--kind",
                        kind,
                        "--report",
                        report,
                        "--format",
                        "json",
                    ],
                    runtime_dir,
                )
                retrieved_at = self._timestamp(f"{kind}.retrieved_at", requested_at)
                previous = retrieved_at
                artifact = FinancialRawArtifact(
                    source=command_source,
                    path=f"raw/financial-statement-{kind.lower()}.json",
                    payload=result.stdout,
                    sha256=hashlib.sha256(result.stdout).hexdigest(),
                    requested_at=requested_at,
                    retrieved_at=retrieved_at,
                )
                completed.append((kind, command_source, result, artifact))
                if result.stderr:
                    warnings.append(
                        ProviderWarningMetadata(
                            source=f"{command_source}.stderr",
                            sha256=hashlib.sha256(result.stderr).hexdigest(),
                            bytes=len(result.stderr),
                        )
                    )

            if include_segments:
                command_source = "cli.business-segments"
                requested_at = self._timestamp("segments.requested_at", previous)
                result = self._run_provider(
                    [
                        str(binary),
                        "business-segments",
                        normalized_symbol,
                        "--history",
                        "--report",
                        report,
                        "--format",
                        "json",
                    ],
                    runtime_dir,
                )
                retrieved_at = self._timestamp("segments.retrieved_at", requested_at)
                previous = retrieved_at
                artifact = FinancialRawArtifact(
                    source=command_source,
                    path="raw/business-segments.json",
                    payload=result.stdout,
                    sha256=hashlib.sha256(result.stdout).hexdigest(),
                    requested_at=requested_at,
                    retrieved_at=retrieved_at,
                )
                completed.append(("segments", command_source, result, artifact))
                if result.stderr:
                    warnings.append(
                        ProviderWarningMetadata(
                            source=f"{command_source}.stderr",
                            sha256=hashlib.sha256(result.stderr).hexdigest(),
                            bytes=len(result.stderr),
                        )
                    )

        decoded = [
            (kind, source, _decode_json(result.stdout), artifact)
            for kind, source, result, artifact in completed
        ]
        statement_payloads = decoded[: len(STATEMENT_KINDS)]
        empty_count = 0
        for _kind, _source, payload, _artifact in statement_payloads:
            periods = _validate_statement_envelope(payload, report)
            if not periods:
                empty_count += 1
        if empty_count == len(STATEMENT_KINDS):
            raise LongbridgeCollectionError(
                "no_data", "Longbridge returned no financial statement data", retryable=True
            )
        if empty_count:
            raise LongbridgeCollectionError(
                "partial_result", "Longbridge omitted one or more required financial statements"
            )

        statements = tuple(
            _normalize_statement(
                payload,
                statement_kind=kind,  # type: ignore[arg-type]
                expected_report=report,
            )
            for kind, _source, payload, _artifact in statement_payloads
        )
        _validate_statement_set(statements)
        segments = (
            _normalize_segments(decoded[-1][2], report) if include_segments else None
        )
        available_at = self._timestamp("available_at", previous)
        artifacts = tuple(item[3] for item in decoded)
        evidence: list[Evidence] = []
        for statement, (_kind, _source, _payload, artifact) in zip(
            statements, statement_payloads, strict=True
        ):
            evidence.append(
                _build_snapshot_evidence(
                    statement,
                    symbol=normalized_symbol,
                    cli_version=cli_version,
                    retrieved_at=artifact.retrieved_at,
                    available_at=available_at,
                    artifact=artifact,
                )
            )
        if segments is not None:
            segment_artifact = artifacts[-1]
            evidence.append(
                _build_snapshot_evidence(
                    segments,
                    symbol=normalized_symbol,
                    cli_version=cli_version,
                    retrieved_at=segment_artifact.retrieved_at,
                    available_at=available_at,
                    artifact=segment_artifact,
                )
            )
        evidence_tuple = tuple(evidence)
        if len(_evidence_bundle_bytes(evidence_tuple)) > _MAX_FINANCIAL_EVIDENCE_BUNDLE_BYTES:
            raise LongbridgeCollectionError(
                "output_too_large",
                "normalized Longbridge financial evidence exceeds the merge input limit",
            )
        return FinancialCollection(
            symbol=normalized_symbol,
            report=report,
            include_segments=include_segments,
            statements=statements,
            segments=segments,
            artifacts=artifacts,
            evidence=evidence_tuple,
            requested_at=collection_requested_at,
            retrieved_at=artifacts[-1].retrieved_at,
            available_at=available_at,
            cli_version=cli_version,
            warnings=tuple(warnings),
        )


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _evidence_bundle_bytes(evidence: tuple[Evidence, ...]) -> bytes:
    payload = EvidenceBundle(evidence=list(evidence)).model_dump(mode="json")
    return _json_bytes(payload)


def _validate_collection(collection: FinancialCollection) -> tuple[Evidence, ...]:
    try:
        _validate_statement_set(collection.statements)
    except LongbridgeCollectionError as exc:
        raise ValueError("financial collection statement set is invalid") from exc
    if collection.include_segments != (collection.segments is not None):
        raise ValueError("financial collection segment flag does not match segment data")
    if any(item.report != collection.report for item in collection.statements):
        raise ValueError("financial collection report does not match its statements")
    if collection.segments is not None and collection.segments.report != collection.report:
        raise ValueError("financial collection report does not match its segments")
    if re.fullmatch(r"longbridge [0-9A-Za-z.+-]+", collection.cli_version) is None:
        raise ValueError("financial collection has an invalid CLI version")
    expected_count = len(STATEMENT_KINDS) + int(collection.include_segments)
    if len(collection.artifacts) != expected_count or len(collection.evidence) != expected_count:
        raise ValueError("financial collection artifact/evidence cardinality mismatch")
    expected_paths = {
        "raw/financial-statement-is.json",
        "raw/financial-statement-bs.json",
        "raw/financial-statement-cf.json",
    }
    if collection.include_segments:
        expected_paths.add("raw/business-segments.json")
    artifact_by_path: dict[str, FinancialRawArtifact] = {}
    for artifact in collection.artifacts:
        if artifact.path in artifact_by_path or artifact.path not in expected_paths:
            raise ValueError("financial collection contains an invalid raw artifact path")
        if hashlib.sha256(artifact.payload).hexdigest() != artifact.sha256:
            raise ValueError("financial collection raw artifact hash mismatch")
        if (
            artifact.requested_at.tzinfo is None
            or artifact.requested_at.utcoffset() is None
            or artifact.retrieved_at.tzinfo is None
            or artifact.retrieved_at.utcoffset() is None
            or artifact.retrieved_at < artifact.requested_at
        ):
            raise ValueError("financial collection raw artifact has an invalid timeline")
        artifact_by_path[artifact.path] = artifact
    if set(artifact_by_path) != expected_paths:
        raise ValueError("financial collection is missing a raw artifact")
    validated = tuple(validate_evidence_records(list(collection.evidence)))
    if (
        collection.requested_at != collection.artifacts[0].requested_at
        or collection.retrieved_at != collection.artifacts[-1].retrieved_at
        or collection.available_at.tzinfo is None
        or collection.available_at.utcoffset() is None
        or collection.available_at < collection.retrieved_at
    ):
        raise ValueError("financial collection has an invalid collection timeline")
    expected_evidence: list[Evidence] = []
    for statement in collection.statements:
        path = f"raw/financial-statement-{statement.statement_kind.lower()}.json"
        artifact = artifact_by_path[path]
        if artifact.source != f"cli.financial-statement.{statement.statement_kind}":
            raise ValueError("financial statement artifact source does not match its kind")
        try:
            replayed = _normalize_statement(
                _decode_json(artifact.payload),
                statement_kind=statement.statement_kind,
                expected_report=collection.report,
            )
        except LongbridgeCollectionError as exc:
            raise ValueError("financial statement raw artifact is invalid") from exc
        if replayed != statement:
            raise ValueError("financial statement normalization does not match its raw artifact")
        expected_evidence.append(
            _build_snapshot_evidence(
                statement,
                symbol=collection.symbol,
                cli_version=collection.cli_version,
                retrieved_at=artifact.retrieved_at,
                available_at=collection.available_at,
                artifact=artifact,
            )
        )
    if collection.segments is not None:
        artifact = artifact_by_path["raw/business-segments.json"]
        if artifact.source != "cli.business-segments":
            raise ValueError("business segment artifact has an invalid source")
        try:
            replayed_segments = _normalize_segments(
                _decode_json(artifact.payload), collection.report
            )
        except LongbridgeCollectionError as exc:
            raise ValueError("business segment raw artifact is invalid") from exc
        if replayed_segments != collection.segments:
            raise ValueError("business segment normalization does not match its raw artifact")
        expected_evidence.append(
            _build_snapshot_evidence(
                collection.segments,
                symbol=collection.symbol,
                cli_version=collection.cli_version,
                retrieved_at=artifact.retrieved_at,
                available_at=collection.available_at,
                artifact=artifact,
            )
        )
    if validated != tuple(expected_evidence):
        raise ValueError("financial evidence does not match the normalized collection")
    return validated


def _validate_staged_financial_output(
    root_fd: int,
    raw_fd: int,
    files: Mapping[str, bytes],
    evidence: tuple[Evidence, ...],
) -> None:
    if set(os.listdir(root_fd)) != {"raw", "evidence.json", "collection.json"}:
        raise OSError("staged financial output contains unexpected entries")
    fresh_raw_fd = _open_directory_at(root_fd, "raw")
    try:
        if not _directory_entry_matches_fd(root_fd, "raw", raw_fd):
            raise OSError("staged financial raw directory changed")
        expected_raw = {Path(path).name for path in files if path.startswith("raw/")}
        if set(os.listdir(fresh_raw_fd)) != expected_raw:
            raise OSError("staged financial raw artifact set mismatch")
        for relative_path in sorted(path for path in files if path.startswith("raw/")):
            payload = files[relative_path]
            actual = _hash_file_at(
                fresh_raw_fd, Path(relative_path).name, len(payload)
            )
            if actual != hashlib.sha256(payload).hexdigest():
                raise OSError("staged financial raw artifact hash mismatch")
    finally:
        os.close(fresh_raw_fd)
    for name in ("evidence.json", "collection.json"):
        expected = files[name]
        if _read_file_at(root_fd, name, len(expected)) != expected:
            raise OSError("staged financial metadata content mismatch")
    staged_bundle = EvidenceBundle.model_validate_json(files["evidence.json"])
    if tuple(validate_evidence_records(staged_bundle.evidence)) != evidence:
        raise OSError("staged financial evidence validation mismatch")


def _publish_financial_files(
    output_dir: Path,
    files: Mapping[str, bytes],
    evidence: tuple[Evidence, ...],
) -> tuple[Path, bool]:
    output_dir = Path(output_dir)
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output path already exists: {output_dir}")
    try:
        _require_safe_filesystem_capabilities()
    except EvidenceMergeError as exc:
        raise OSError(str(exc)) from exc
    parent_fd: int | None = None
    root_fd: int | None = None
    raw_fd: int | None = None
    staging_name: str | None = None
    published: Path | None = None
    durability_confirmed = True
    try:
        parent_path, parent_fd = _open_output_parent(output_dir)
        output_name = output_dir.name
        if not output_name or output_name in {".", ".."}:
            raise OSError("output path is invalid")
        published = parent_path / output_name
        if _entry_exists_at(parent_fd, output_name):
            raise FileExistsError(f"output path already exists: {output_dir}")
        if not _directory_path_matches_fd(parent_path, parent_fd):
            raise OSError("output parent changed")
        staging_name, root_fd = _create_private_staging_at(parent_fd)
        if not _directory_entry_matches_fd(parent_fd, staging_name, root_fd):
            raise OSError("private staging directory changed")
        raw_fd = _create_directory_at(root_fd, "raw")
        for relative_path, payload in sorted(files.items()):
            path = Path(relative_path)
            destination_fd = raw_fd if path.parts[0] == "raw" else root_fd
            _write_file_at_fsynced(destination_fd, path.name, payload)
        _validate_staged_financial_output(root_fd, raw_fd, files, evidence)
        os.fsync(raw_fd)
        os.fsync(root_fd)
        os.fsync(parent_fd)
        if _entry_exists_at(parent_fd, output_name):
            raise FileExistsError(f"output path already exists: {output_dir}")
        if not _directory_path_matches_fd(parent_path, parent_fd):
            raise OSError("output parent changed")
        if not _directory_entry_matches_fd(parent_fd, staging_name, root_fd):
            raise OSError("private staging directory changed")
        _rename_directory_noreplace_at(parent_fd, staging_name, output_name, published)
        try:
            os.fsync(parent_fd)
        except (OSError, RuntimeError):
            durability_confirmed = False
        try:
            if not _directory_entry_matches_fd(parent_fd, output_name, root_fd):
                raise OSError("published financial directory identity mismatch")
            _validate_staged_financial_output(root_fd, raw_fd, files, evidence)
            if not _directory_path_matches_fd(parent_path, parent_fd):
                raise OSError("output parent changed")
        except (EvidenceMergeError, OSError, RuntimeError, ValueError) as exc:
            if not _directory_entry_matches_fd(parent_fd, output_name, root_fd):
                raise OSError("published financial output failed final validation") from exc
            try:
                _rename_directory_noreplace_at(
                    parent_fd,
                    output_name,
                    staging_name,
                    parent_path / staging_name,
                )
            except OSError as rollback_exc:
                raise OSError(
                    "published financial output failed validation and rollback"
                ) from rollback_exc
            with suppress(OSError, RuntimeError):
                os.fsync(parent_fd)
            raise OSError("published financial output failed final validation") from exc
        staging_name = None
    except FileExistsError:
        raise
    except EvidenceMergeError as exc:
        raise OSError("failed to publish financial evidence") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise OSError("failed to publish financial evidence") from exc
    finally:
        for descriptor in (raw_fd, root_fd, parent_fd):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
        # Failed private staging trees are deliberately retained. Deleting a
        # path after a concurrent same-UID replacement could remove unrelated data.
        del staging_name
    if published is None:
        raise OSError("failed to publish financial evidence")
    return published, durability_confirmed


def write_financial_collection(
    collection: FinancialCollection, output_dir: Path
) -> FinancialCollectionWriteResult:
    evidence = _validate_collection(collection)
    files: dict[str, bytes] = {
        artifact.path: artifact.payload for artifact in collection.artifacts
    }
    files["evidence.json"] = _evidence_bundle_bytes(evidence)
    if len(files["evidence.json"]) > _MAX_FINANCIAL_EVIDENCE_BUNDLE_BYTES:
        raise ValueError("financial evidence exceeds the default merge input limit")
    files["collection.json"] = _json_bytes(collection.manifest())
    published, durability_confirmed = _publish_financial_files(
        output_dir, files, evidence
    )
    return FinancialCollectionWriteResult(
        evidence_path=published / "evidence.json",
        manifest_path=published / "collection.json",
        durability_confirmed=durability_confirmed,
    )
