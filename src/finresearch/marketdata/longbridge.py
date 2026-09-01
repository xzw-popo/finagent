from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any, BinaryIO

from finresearch.evidence import (
    evidence_record_sha256,
    load_evidence,
    validate_evidence_records,
)
from finresearch.schemas import Evidence, EvidenceProvenance, StrictModel

NORMALIZER_VERSION = "longbridge.quote.v1"
SOURCE_URI = "https://open.longbridge.com/docs/quote/pull/quote"
MAX_SYMBOL_LENGTH = 48

_US_CODE = r"(?:\.[A-Z0-9]+|[A-Z0-9][A-Z0-9.-]{0,30})"
_SYMBOL_PATTERNS = {
    "US": re.compile(rf"^{_US_CODE}\.US$"),
    # Hong Kong quote symbols include both numeric securities (700.HK) and
    # alphanumeric indices (HSI.HK). Keeping the first character alphanumeric
    # prevents a symbol from being interpreted as a command-line option.
    "HK": re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,20}\.HK$"),
    "SH": re.compile(r"^[0-9]{6}\.SH$"),
    "SZ": re.compile(r"^[0-9]{6}\.SZ$"),
    "SG": re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,20}\.SG$"),
    "HAS": re.compile(r"^[A-Z0-9]{3,30}\.HAS$"),
}
_SAFE_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "COMSPEC",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TMPDIR",
        "USER",
        "WINDIR",
    }
)
_MISSING = object()


class LongbridgeCollectionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.exit_code = exit_code


@dataclass(frozen=True)
class LongbridgeCollectorConfig:
    timeout_seconds: float = 20.0
    max_symbols: int = 50
    max_stdout_bytes: int = 1_048_576
    max_stderr_bytes: int = 65_536
    region: str = "auto"

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("Longbridge timeout must be between 0 and 120 seconds")
        if self.max_symbols < 1:
            raise ValueError("Longbridge max_symbols must be positive")
        if self.max_stdout_bytes < 1 or self.max_stderr_bytes < 1:
            raise ValueError("Longbridge output limits must be positive")
        if self.region not in {"auto", "cn", "global"}:
            raise ValueError("Longbridge region must be one of: auto, cn, global")


class NormalizedQuote(StrictModel):
    symbol: str
    last: str | None
    change_value: str | None
    change_percentage: str | None
    prev_close: str | None
    open: str | None
    high: str | None
    low: str | None
    volume: str | None
    turnover: str | None
    status: str
    pre_market: dict[str, Any] | None
    post_market: dict[str, Any] | None
    overnight: dict[str, Any] | None


@dataclass(frozen=True)
class ProviderWarningMetadata:
    source: str
    sha256: str
    bytes: int

    def manifest(self) -> dict[str, str | int]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class QuoteCollection:
    symbols: tuple[str, ...]
    quotes: tuple[NormalizedQuote, ...]
    evidence: tuple[Evidence, ...]
    requested_at: datetime
    retrieved_at: datetime
    available_at: datetime
    cli_version: str
    raw_response: bytes
    raw_sha256: str
    warnings: tuple[ProviderWarningMetadata, ...]

    def manifest(self) -> dict[str, Any]:
        duration_ms = int((self.retrieved_at - self.requested_at).total_seconds() * 1000)
        return {
            "schema": "finresearch.longbridge_quote_collection.v1",
            "provider": "longbridge",
            "provider_cli_version": self.cli_version,
            "command": "quote",
            "symbols": list(self.symbols),
            "requested_at": _iso_utc(self.requested_at),
            "retrieved_at": _iso_utc(self.retrieved_at),
            "available_at": _iso_utc(self.available_at),
            "duration_ms": duration_ms,
            "raw_response": {
                "path": "raw-response.json",
                "sha256": self.raw_sha256,
                "bytes": len(self.raw_response),
            },
            "evidence": {
                "path": "evidence.json",
                "evidence_ids": [item.evidence_id for item in self.evidence],
            },
            "time_semantics": {
                "source_event_at": "unavailable_for_combined_quote_snapshot",
                "known_at": "collector_received_complete_response",
                "available_at": "normalization_completed",
            },
            "freshness": "unknown_account_entitlement",
            "warnings": [warning.manifest() for warning in self.warnings],
        }


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LongbridgeCollectionError("clock_error", f"{label} must include a timezone")
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_symbols(symbols: Sequence[str], max_symbols: int = 50) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        if not isinstance(raw_symbol, str) or not raw_symbol:
            raise LongbridgeCollectionError("invalid_symbol", "symbol must not be empty")
        if raw_symbol != raw_symbol.strip():
            raise LongbridgeCollectionError(
                "invalid_symbol", f"symbol contains surrounding whitespace: {raw_symbol!r}"
            )
        if not raw_symbol.isascii() or len(raw_symbol) > MAX_SYMBOL_LENGTH:
            raise LongbridgeCollectionError(
                "invalid_symbol", f"invalid Longbridge symbol: {raw_symbol!r}"
            )
        symbol = raw_symbol.upper()
        suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
        pattern = _SYMBOL_PATTERNS.get(suffix)
        if pattern is None or pattern.fullmatch(symbol) is None:
            raise LongbridgeCollectionError(
                "invalid_symbol",
                f"symbol must use an explicit supported suffix: {raw_symbol!r}",
            )
        if symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    if not normalized:
        raise LongbridgeCollectionError("invalid_symbol", "at least one symbol is required")
    if len(normalized) > max_symbols:
        raise LongbridgeCollectionError(
            "invalid_symbol", f"at most {max_symbols} unique symbols are allowed per request"
        )
    return tuple(normalized)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_external_text(data: bytes, limit: int) -> str:
    text = data[:limit].decode("utf-8", errors="replace")
    # Redact credential-bearing headers before generic assignments so the
    # complete Cookie / Authorization value is removed, including spaces and
    # semicolon-delimited attributes.
    text = re.sub(
        r"(?im)((?<![A-Za-z0-9_-])[\"']?"
        r"(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?"
        r"[ \t]*[:=][ \t]*)[^\r\n]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:authorization|proxy-authorization)[ \t]+"
        r"(?:bearer|basic)[ \t]+)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    # JSON diagnostics and key=value diagnostics use several common token
    # spellings. Quotes around keys and values are optional here because this
    # is display sanitization, not JSON parsing.
    text = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|"
        r"session|api[_-]?key|secret|password)[\"']?[ \t]*[:=][ \t]*)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
        r"\1[REDACTED]",
        text,
    )
    # Proxy errors can echo credentials embedded in proxy URLs even when no
    # credential key name is present.
    text = re.sub(
        r"(?i)\b(https?|socks5?|socks5h)://[^/@\s]+@",
        r"\1://[REDACTED]@",
        text,
    )
    text = re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[REDACTED]", text)
    home = os.environ.get("HOME")
    if home:
        text = text.replace(home, "[HOME]")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "?", text)
    return text.strip()


def _classify_failure(stderr: bytes, exit_code: int, limit: int) -> LongbridgeCollectionError:
    safe = _safe_external_text(stderr, limit) or "Longbridge quote command failed"
    lowered = safe.lower()
    if "not logged in" in lowered or "unauthorized" in lowered:
        code, retryable = "authentication_required", False
    elif "param_error" in lowered or "invalid symbol" in lowered:
        code, retryable = "invalid_symbol", False
    elif "rate limit" in lowered or "429" in lowered:
        code, retryable = "rate_limited", True
    elif "mainland access point" in lowered or "longbridge_region" in lowered:
        code, retryable = "region_unreachable", False
    elif any(term in lowered for term in ("connect", "dns", "tls", "sending request")):
        code, retryable = "network_unavailable", True
    else:
        code, retryable = "command_failed", False
    message = {
        "authentication_required": "Longbridge authentication is required",
        "invalid_symbol": "Longbridge rejected one or more symbols",
        "rate_limited": "Longbridge rate limit was reached",
        "region_unreachable": "The configured Longbridge region endpoint is unavailable",
        "network_unavailable": "Longbridge could not reach its market data service",
        "command_failed": "Longbridge quote command failed",
    }[code]
    return LongbridgeCollectionError(
        code,
        message,
        retryable=retryable,
        exit_code=exit_code,
    )


def _read_bounded_pipe(
    pipe: BinaryIO,
    *,
    limit: int,
    buffer: bytearray,
    exceeded: threading.Event,
    stop: threading.Event,
    errors: list[BaseException],
) -> None:
    try:
        while not stop.is_set():
            # os.read returns as soon as bytes are available instead of waiting
            # for a buffered ``read(n)`` to fill a large request. Request no
            # more than the remaining allowance plus the one sentinel byte
            # needed to prove the limit was exceeded.
            read_size = min(65_536, max(1, limit + 1 - len(buffer)))
            chunk = os.read(pipe.fileno(), read_size)
            if not chunk:
                break
            remaining = limit + 1 - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(buffer) > limit:
                exceeded.set()
                stop.set()
                break
    except BaseException as exc:  # pragma: no cover - OS pipe failures are rare.
        errors.append(exc)
        stop.set()
    finally:
        pipe.close()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    with suppress(OSError):
        process.kill()


def _bounded_run(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        argv,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        close_fds=True,
        start_new_session=os.name == "posix",
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - PIPE guarantees.
        process.kill()
        process.wait()
        raise OSError("failed to open Longbridge subprocess pipes")

    stdout = bytearray()
    stderr = bytearray()
    stdout_exceeded = threading.Event()
    stderr_exceeded = threading.Event()
    stop = threading.Event()
    errors: list[BaseException] = []
    readers = (
        threading.Thread(
            target=_read_bounded_pipe,
            kwargs={
                "pipe": process.stdout,
                "limit": stdout_limit,
                "buffer": stdout,
                "exceeded": stdout_exceeded,
                "stop": stop,
                "errors": errors,
            },
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded_pipe,
            kwargs={
                "pipe": process.stderr,
                "limit": stderr_limit,
                "buffer": stderr,
                "exceeded": stderr_exceeded,
                "stop": stop,
                "errors": errors,
            },
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if stop.wait(min(0.05, remaining)):
                break
        if timed_out or stop.is_set():
            _terminate_process_tree(process)
        try:
            return_code = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)
            try:
                return_code = process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL should win.
                return_code = process.returncode if process.returncode is not None else -1

        for reader in readers:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                reader.join(remaining)
        if any(reader.is_alive() for reader in readers):
            timed_out = True
            stop.set()
            _terminate_process_tree(process)
            for pipe in (process.stdout, process.stderr):
                with suppress(OSError):
                    pipe.close()
            for reader in readers:
                reader.join(0.25)
    finally:
        if process.poll() is None:  # pragma: no cover - defensive cleanup.
            _terminate_process_tree(process)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)

    if timed_out:
        raise LongbridgeCollectionError(
            "timeout", "Longbridge command timed out", retryable=True
        )
    if stdout_exceeded.is_set():
        raise LongbridgeCollectionError(
            "output_too_large", "Longbridge stdout exceeded the configured size limit"
        )
    if stderr_exceeded.is_set():
        raise LongbridgeCollectionError(
            "output_too_large", "Longbridge stderr exceeded the configured size limit"
        )
    if errors:
        raise OSError(f"failed to read Longbridge subprocess output: {errors[0]}")
    return subprocess.CompletedProcess(
        argv,
        return_code,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
    )


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _validate_json_shape(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > 10_000:
        raise ValueError("JSON response contains too many values")
    if depth > 12:
        raise ValueError("JSON response nesting is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _validate_json_shape(item, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        for item in value:
            _validate_json_shape(item, depth=depth + 1, counter=counter)
    elif value is not None and not isinstance(value, (str, int, float, Decimal, bool)):
        raise ValueError("JSON response contains an unsupported value")


def _decode_json(data: bytes) -> Any:
    if not data:
        raise LongbridgeCollectionError(
            "empty_output", "Longbridge returned an empty response", retryable=True
        )
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=Decimal,
            parse_constant=_reject_nonfinite,
        )
        _validate_json_shape(value)
        return value
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        DecimalException,
        RecursionError,
        ValueError,
    ):
        # Parser errors may embed provider-controlled keys or values. Do not
        # reflect response fragments into CLI-visible error messages.
        raise LongbridgeCollectionError(
            "invalid_json", "Longbridge returned invalid JSON"
        ) from None


def _alias(raw: dict[str, Any], current: str, legacy: str | None = None) -> Any:
    if current in raw:
        return raw[current]
    if legacy is not None and legacy in raw:
        return raw[legacy]
    return _MISSING


def _decimal_string(value: Any, field: str, *, nonnegative: bool) -> str | None:
    if value is _MISSING or value is None or value == "" or value == "-":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge field {field!r} is not numeric"
        )
    try:
        number = Decimal(str(value).strip())
    except (DecimalException, ValueError):
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge field {field!r} is not numeric"
        ) from None
    if not number.is_finite():
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge field {field!r} must be finite"
        )
    if nonnegative and number < 0:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge field {field!r} must not be negative"
        )
    digits = number.as_tuple().digits
    exponent = number.as_tuple().exponent
    if len(digits) > 100 or not -100 <= exponent <= 100:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge field {field!r} exceeds numeric limits"
        )
    return format(number, "f")


def _extended_session(raw: dict[str, Any], current: str, legacy: str) -> dict[str, Any] | None:
    value = _alias(raw, current, legacy)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, dict):
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge field {current!r} must be an object or null"
        )
    _validate_json_shape(value)
    return value


def _normalize_quote(raw: Any) -> NormalizedQuote:
    if not isinstance(raw, dict):
        raise LongbridgeCollectionError(
            "schema_mismatch", "each Longbridge quote result must be an object"
        )
    raw_symbol = raw.get("symbol")
    if not isinstance(raw_symbol, str):
        raise LongbridgeCollectionError(
            "schema_mismatch", "Longbridge quote result is missing symbol"
        )
    try:
        symbol = normalize_symbols([raw_symbol], max_symbols=1)[0]
    except LongbridgeCollectionError as exc:
        if exc.code != "invalid_symbol":  # pragma: no cover - currently exhaustive.
            raise
        raise LongbridgeCollectionError(
            "protocol_mismatch",
            f"Longbridge returned an invalid quote symbol: {raw_symbol!r}",
        ) from None
    if _alias(raw, "last", "last_done") is _MISSING:
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge quote for {symbol} is missing last price"
        )
    status_value = _alias(raw, "status", "trade_status")
    if not isinstance(status_value, str) or not status_value.strip():
        raise LongbridgeCollectionError(
            "schema_mismatch", f"Longbridge quote for {symbol} is missing trade status"
        )
    return NormalizedQuote(
        symbol=symbol,
        last=_decimal_string(_alias(raw, "last", "last_done"), "last", nonnegative=True),
        change_value=_decimal_string(
            _alias(raw, "change_value"), "change_value", nonnegative=False
        ),
        change_percentage=_decimal_string(
            _alias(raw, "change_percentage"), "change_percentage", nonnegative=False
        ),
        prev_close=_decimal_string(
            _alias(raw, "prev_close"), "prev_close", nonnegative=True
        ),
        open=_decimal_string(_alias(raw, "open"), "open", nonnegative=True),
        high=_decimal_string(_alias(raw, "high"), "high", nonnegative=True),
        low=_decimal_string(_alias(raw, "low"), "low", nonnegative=True),
        volume=_decimal_string(_alias(raw, "volume"), "volume", nonnegative=True),
        turnover=_decimal_string(_alias(raw, "turnover"), "turnover", nonnegative=True),
        status=status_value.strip(),
        pre_market=_extended_session(raw, "pre_market", "pre_market_quote"),
        post_market=_extended_session(raw, "post_market", "post_market_quote"),
        overnight=_extended_session(raw, "overnight", "overnight_quote"),
    )


def _build_evidence(
    quote: NormalizedQuote,
    *,
    cli_version: str,
    retrieved_at: datetime,
    available_at: datetime,
    raw_sha256: str,
) -> Evidence:
    excerpt_payload = {
        "schema": NORMALIZER_VERSION,
        "provider": "longbridge",
        "symbol": quote.symbol,
        "captured_at": _iso_utc(retrieved_at),
        "available_at": _iso_utc(available_at),
        "time_basis": "collector_received_at; source_event_at unavailable",
        "quote": quote.model_dump(mode="json"),
    }
    excerpt = _canonical_json_bytes(excerpt_payload).decode("utf-8")
    normalized_sha256 = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    slug = re.sub(r"[^a-z0-9]+", "-", quote.symbol.lower()).strip("-")
    timestamp = available_at.strftime("%Y%m%dT%H%M%S%fZ")
    evidence_id = f"lbq-{slug}-{timestamp}-{normalized_sha256[:12]}"
    provenance = EvidenceProvenance(
        provider="longbridge",
        source_type="market_quote_snapshot",
        source_endpoint="cli.quote",
        symbol=quote.symbol,
        observed_at=retrieved_at,
        source_event_at=None,
        freshness="unknown",
        raw_artifact_ref="raw-response.json",
        raw_sha256=raw_sha256,
        normalized_sha256=normalized_sha256,
        normalizer_version=NORMALIZER_VERSION,
    )
    locator = (
        f"provider=longbridge; endpoint=cli.quote; symbol={quote.symbol}; "
        f"cli_version={cli_version}; observed_at={_iso_utc(retrieved_at)}; "
        f"source_event_at=unavailable; schema={NORMALIZER_VERSION}"
    )
    draft = Evidence(
        evidence_type="market_quote_snapshot",
        evidence_id=evidence_id,
        title=f"长桥证券行情快照：{quote.symbol}",
        publisher="长桥证券",
        uri=SOURCE_URI,
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


class LongbridgeQuoteCollector:
    def __init__(
        self,
        config: LongbridgeCollectorConfig | None = None,
        *,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self.config = config or LongbridgeCollectorConfig()
        self._clock = clock

    def _resolve_binary(self) -> Path:
        resolved = shutil.which("longbridge")
        if resolved is None:
            raise LongbridgeCollectionError(
                "binary_not_found",
                "Longbridge CLI was not found on PATH; install and authenticate it first",
            )
        try:
            path = Path(resolved).resolve(strict=True)
        except (OSError, RuntimeError):
            raise LongbridgeCollectionError(
                "binary_not_found", "Longbridge CLI disappeared or could not be resolved"
            ) from None
        if not path.is_file() or not os.access(path, os.X_OK):
            raise LongbridgeCollectionError(
                "binary_not_found", "Longbridge CLI is not an executable file"
            )
        return path

    def _environment(self) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS
        }
        if self.config.region != "auto":
            environment["LONGBRIDGE_REGION"] = self.config.region
        return environment

    def _run(self, argv: list[str], runtime_dir: str) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = _bounded_run(
                argv,
                cwd=runtime_dir,
                env=self._environment(),
                timeout=self.config.timeout_seconds,
                stdout_limit=self.config.max_stdout_bytes,
                stderr_limit=self.config.max_stderr_bytes,
            )
        except LongbridgeCollectionError:
            raise
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise LongbridgeCollectionError(
                "binary_not_found", f"Longbridge CLI could not be executed: {exc}"
            ) from None
        if len(completed.stdout) > self.config.max_stdout_bytes:
            raise LongbridgeCollectionError(
                "output_too_large", "Longbridge stdout exceeded the configured size limit"
            )
        if len(completed.stderr) > self.config.max_stderr_bytes:
            raise LongbridgeCollectionError(
                "output_too_large", "Longbridge stderr exceeded the configured size limit"
            )
        if completed.returncode != 0:
            raise _classify_failure(
                completed.stderr,
                completed.returncode,
                self.config.max_stderr_bytes,
            )
        return completed

    def collect(self, symbols: Sequence[str]) -> QuoteCollection:
        normalized_symbols = normalize_symbols(symbols, self.config.max_symbols)
        binary = self._resolve_binary()
        with tempfile.TemporaryDirectory(prefix="finresearch-longbridge-") as runtime_dir:
            version_result = self._run([str(binary), "--version"], runtime_dir)
            try:
                cli_version = version_result.stdout.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError:
                raise LongbridgeCollectionError(
                    "invalid_json", "Longbridge version output is not valid UTF-8"
                ) from None
            if not cli_version:
                raise LongbridgeCollectionError(
                    "schema_mismatch", "Longbridge CLI returned an empty version"
                )
            if re.fullmatch(r"longbridge [0-9A-Za-z.+-]+", cli_version) is None:
                raise LongbridgeCollectionError(
                    "schema_mismatch", "Longbridge CLI returned an invalid version string"
                )

            requested_at = _ensure_utc(self._clock(), "requested_at")
            quote_result = self._run(
                [str(binary), "quote", *normalized_symbols, "--format", "json"],
                runtime_dir,
            )
            retrieved_at = _ensure_utc(self._clock(), "retrieved_at")
            if retrieved_at < requested_at:
                raise LongbridgeCollectionError(
                    "clock_error", "retrieved_at must not be before requested_at"
                )

        payload = _decode_json(quote_result.stdout)
        if not isinstance(payload, list):
            raise LongbridgeCollectionError(
                "schema_mismatch", "Longbridge quote response must be a JSON array"
            )
        if not payload:
            raise LongbridgeCollectionError(
                "no_data",
                "Longbridge returned no quote data for the requested symbols",
                retryable=True,
            )
        quotes = tuple(_normalize_quote(item) for item in payload)
        by_symbol: dict[str, NormalizedQuote] = {}
        for quote in quotes:
            if quote.symbol in by_symbol:
                raise LongbridgeCollectionError(
                    "protocol_mismatch",
                    f"Longbridge returned duplicate symbol {quote.symbol}",
                )
            by_symbol[quote.symbol] = quote
        requested_symbol_set = set(normalized_symbols)
        returned_symbol_set = set(by_symbol)
        extra = sorted(returned_symbol_set - requested_symbol_set)
        if extra:
            raise LongbridgeCollectionError(
                "protocol_mismatch",
                f"Longbridge returned unrequested symbols: {extra}",
            )
        missing = sorted(requested_symbol_set - returned_symbol_set)
        if missing:
            raise LongbridgeCollectionError(
                "partial_result",
                f"Longbridge omitted requested symbols: {missing}",
            )
        ordered_quotes = tuple(by_symbol[symbol] for symbol in normalized_symbols)
        available_at = _ensure_utc(self._clock(), "available_at")
        if available_at < retrieved_at:
            raise LongbridgeCollectionError(
                "clock_error", "available_at must not be before retrieved_at"
            )
        raw_sha256 = hashlib.sha256(quote_result.stdout).hexdigest()
        evidence = tuple(
            _build_evidence(
                quote,
                cli_version=cli_version,
                retrieved_at=retrieved_at,
                available_at=available_at,
                raw_sha256=raw_sha256,
            )
            for quote in ordered_quotes
        )
        warning_values = []
        for source, stderr in (
            ("cli.version.stderr", version_result.stderr),
            ("cli.quote.stderr", quote_result.stderr),
        ):
            if stderr:
                warning_values.append(
                    ProviderWarningMetadata(
                        source=source,
                        sha256=hashlib.sha256(stderr).hexdigest(),
                        bytes=len(stderr),
                    )
                )
        return QuoteCollection(
            symbols=normalized_symbols,
            quotes=ordered_quotes,
            evidence=evidence,
            requested_at=requested_at,
            retrieved_at=retrieved_at,
            available_at=available_at,
            cli_version=cli_version,
            raw_response=quote_result.stdout,
            raw_sha256=raw_sha256,
            warnings=tuple(warning_values),
        )


def write_quote_collection(collection: QuoteCollection, output_dir: Path) -> Path:
    raw_sha256 = hashlib.sha256(collection.raw_response).hexdigest()
    if raw_sha256 != collection.raw_sha256:
        raise ValueError("quote collection raw_sha256 mismatch")
    validated_evidence = validate_evidence_records(list(collection.evidence))
    if tuple(quote.symbol for quote in collection.quotes) != collection.symbols:
        raise ValueError("quote collection symbols do not match normalized quotes")
    evidence_symbols = tuple(
        item.provenance.symbol if item.provenance is not None else ""
        for item in validated_evidence
    )
    if evidence_symbols != collection.symbols:
        raise ValueError("quote collection symbols do not match evidence provenance")
    if any(
        item.provenance is None or item.provenance.raw_sha256 != raw_sha256
        for item in validated_evidence
    ):
        raise ValueError("quote collection evidence does not match the raw response")
    if any(
        item.provenance is None
        or item.provenance.raw_artifact_ref != "raw-response.json"
        for item in validated_evidence
    ):
        raise ValueError("quote collection evidence has an invalid raw artifact reference")
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output path already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent)
    )
    output_created = False
    try:
        (temporary / "raw-response.json").write_bytes(collection.raw_response)
        evidence_payload = {
            "evidence": [item.model_dump(mode="json") for item in collection.evidence]
        }
        (temporary / "evidence.json").write_text(
            json.dumps(evidence_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (temporary / "collection.json").write_text(
            json.dumps(collection.manifest(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        load_evidence(temporary / "evidence.json")
        if os.path.lexists(output_dir):
            raise FileExistsError(f"output path already exists: {output_dir}")
        output_dir.mkdir(exist_ok=False)
        output_created = True
        for child in temporary.iterdir():
            child.rename(output_dir / child.name)
        temporary.rmdir()
    except Exception:
        if output_created and output_dir.exists():
            shutil.rmtree(output_dir)
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return output_dir / "evidence.json"
