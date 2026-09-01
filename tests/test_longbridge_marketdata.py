import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import finresearch.marketdata.longbridge as longbridge_module
from finresearch.evidence import (
    evidence_record_sha256,
    filter_evidence_as_of,
    load_evidence,
)
from finresearch.marketdata.longbridge import (
    LongbridgeCollectionError,
    LongbridgeCollectorConfig,
    LongbridgeQuoteCollector,
    normalize_symbols,
    write_quote_collection,
)
from finresearch.schemas import ResearchRequest

ROOT = Path(__file__).resolve().parents[1]


def _quote(symbol: str = "NVDA.US", *, legacy: bool = False) -> dict:
    quote = {
        "symbol": symbol,
        "change_value": "2.225",
        "change_percentage": "1.02",
        "prev_close": "217.550",
        "open": "218.720",
        "high": "220.600",
        "low": "216.210",
        "volume": 44_949_439,
        "turnover": "9845138061.887",
    }
    if legacy:
        quote.update(
            {
                "last_done": "219.775",
                "trade_status": "Normal",
                "pre_market_quote": None,
                "post_market_quote": None,
                "overnight_quote": None,
            }
        )
    else:
        quote.update(
            {
                "last": "219.775",
                "status": "Normal",
                "pre_market": None,
                "post_market": None,
                "overnight": None,
                "future_provider_field": "ignored by normalized evidence",
            }
        )
    return quote


def _collector_with_fake_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    stdout: bytes,
    stderr: bytes = b"",
    returncode: int = 0,
) -> tuple[LongbridgeQuoteCollector, list[tuple[list[str], dict]]]:
    binary = tmp_path / "longbridge"
    binary.write_text("fake executable", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setattr(longbridge_module.shutil, "which", lambda _: str(binary))
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=b"longbridge 0.28.4\n",
                stderr=b"",
            )
        return subprocess.CompletedProcess(
            argv,
            returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(longbridge_module, "_bounded_run", fake_run)
    times = iter(
        [
            datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 31, 12, 0, 0, 500_000, tzinfo=UTC),
            datetime(2026, 8, 31, 12, 0, 1, tzinfo=UTC),
        ]
    )
    return LongbridgeQuoteCollector(clock=lambda: next(times)), calls


def test_symbol_normalization_is_explicit_and_injection_safe() -> None:
    assert normalize_symbols(
        [
            "aapl.us",
            ".VIX.US",
            "700.HK",
            "hsi.hk",
            "600519.SH",
            "000568.SZ",
            "D05.SG",
        ]
    ) == (
        "AAPL.US",
        ".VIX.US",
        "700.HK",
        "HSI.HK",
        "600519.SH",
        "000568.SZ",
        "D05.SG",
    )
    assert normalize_symbols(["AAPL.US", "aapl.us"]) == ("AAPL.US",)

    for invalid in (
        "AAPL",
        " AAPL.US",
        "AAPL.US ",
        "AAPL.US;order",
        "$(id).US",
        "--HSI.HK",
        "HSI/../../x.HK",
    ):
        with pytest.raises(LongbridgeCollectionError) as error:
            normalize_symbols([invalid])
        assert error.value.code == "invalid_symbol"


def test_collect_quote_builds_round_trip_evidence_and_safe_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_response = (json.dumps([_quote()], indent=2) + "\n").encode()
    collector, calls = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        stdout=raw_response,
        stderr=b"warning: token=should-not-leak",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sentinel-secret")
    monkeypatch.setenv("LONGBRIDGE_ENV", "staging")

    collection = collector.collect(["nvda.us"])

    assert collection.symbols == ("NVDA.US",)
    assert collection.quotes[0].last == "219.775"
    assert collection.quotes[0].volume == "44949439"
    assert collection.raw_response == raw_response
    assert collection.raw_sha256 == hashlib.sha256(raw_response).hexdigest()
    evidence = collection.evidence[0]
    assert evidence.evidence_type == "market_quote_snapshot"
    assert evidence.published_at is None
    assert evidence.known_at == collection.retrieved_at
    assert evidence.available_at == collection.available_at
    assert evidence.provenance is not None
    assert evidence.provenance.source_event_at is None
    assert evidence.provenance.freshness == "unknown"
    assert evidence.provenance.raw_artifact_ref == "raw-response.json"
    assert "future_provider_field" not in evidence.excerpt
    assert "source_event_at unavailable" in evidence.excerpt
    warning = collection.warnings[0]
    assert warning.source == "cli.quote.stderr"
    assert warning.sha256 == hashlib.sha256(
        b"warning: token=should-not-leak"
    ).hexdigest()
    assert warning.bytes == len(b"warning: token=should-not-leak")
    assert "should-not-leak" not in repr(collection.warnings)

    version_argv, version_kwargs = calls[0]
    quote_argv, quote_kwargs = calls[1]
    assert version_argv[1:] == ["--version"]
    assert quote_argv[1:] == ["quote", "NVDA.US", "--format", "json"]
    assert "DEEPSEEK_API_KEY" not in quote_kwargs["env"]
    assert "LONGBRIDGE_ENV" not in quote_kwargs["env"]
    runtime_dir = Path(quote_kwargs["cwd"])
    assert runtime_dir == Path(version_kwargs["cwd"])
    assert not runtime_dir.exists()

    evidence_path = write_quote_collection(collection, tmp_path / "quote-run")
    loaded = load_evidence(evidence_path)
    assert loaded == list(collection.evidence)
    assert (tmp_path / "quote-run/raw-response.json").read_bytes() == raw_response
    manifest = json.loads((tmp_path / "quote-run/collection.json").read_text())
    assert manifest["raw_response"]["sha256"] == collection.raw_sha256
    assert manifest["warnings"] == [
        {
            "source": "cli.quote.stderr",
            "sha256": warning.sha256,
            "bytes": warning.bytes,
        }
    ]
    assert "should-not-leak" not in json.dumps(manifest)

    (tmp_path / "quote-run/raw-response.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="raw_sha256 mismatch"):
        load_evidence(evidence_path)


def test_legacy_longbridge_fields_are_normalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = json.dumps([_quote("700.HK", legacy=True)]).encode()
    collector, _ = _collector_with_fake_cli(monkeypatch, tmp_path, stdout=response)

    quote = collector.collect(["700.HK"]).quotes[0]

    assert quote.last == "219.775"
    assert quote.status == "Normal"
    assert quote.pre_market is None


def test_deeply_nested_json_is_classified_instead_of_escaping_as_recursion_error() -> None:
    nested = b"[" * 5_000 + b"]" * 5_000

    with pytest.raises(LongbridgeCollectionError) as error:
        longbridge_module._decode_json(nested)

    assert error.value.code == "invalid_json"


@pytest.mark.parametrize(
    ("requested", "response", "expected_code"),
    [
        (["NVDA.US"], [], "no_data"),
        (["NVDA.US", "AAPL.US"], [_quote()], "partial_result"),
        (["NVDA.US"], [_quote(), _quote()], "protocol_mismatch"),
        (["NVDA.US"], [_quote("AAPL.US")], "protocol_mismatch"),
        (
            ["NVDA.US"],
            [_quote(), _quote("AAPL.US")],
            "protocol_mismatch",
        ),
        (["NVDA.US"], [_quote("--HELP.HK")], "protocol_mismatch"),
    ],
)
def test_provider_symbol_cardinality_protocol_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested: list[str],
    response: list[dict],
    expected_code: str,
) -> None:
    collector, _ = _collector_with_fake_cli(
        monkeypatch, tmp_path, stdout=json.dumps(response).encode()
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect(requested)

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (b"", "empty_output"),
        (b'{"symbol":"NVDA.US"}', "schema_mismatch"),
        (b'[{"symbol":"NVDA.US","symbol":"AAPL.US"}]', "invalid_json"),
        (b'[{"symbol":"NVDA.US","last":NaN}]', "invalid_json"),
        (
            b'[{"symbol":"NVDA.US","last":1e1000,"status":"Normal"}]',
            "schema_mismatch",
        ),
        (
            b'[{"symbol":"NVDA.US","last":1e999999999999999999999999999999,'
            b'"status":"Normal"}]',
            "invalid_json",
        ),
    ],
)
def test_invalid_provider_output_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: bytes,
    expected_code: str,
) -> None:
    collector, _ = _collector_with_fake_cli(monkeypatch, tmp_path, stdout=response)

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect(["NVDA.US"])

    assert error.value.code == expected_code


def test_provider_failure_is_classified_and_does_not_parse_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    collector, _ = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        stdout=b"not json",
        stderr=b"China Mainland access point unavailable; set LONGBRIDGE_REGION=global",
        returncode=1,
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect(["NVDA.US"])

    assert error.value.code == "region_unreachable"
    assert error.value.exit_code == 1


def test_provider_failure_redacts_headers_json_tokens_and_proxy_userinfo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets = (
        "basic-credential",
        "direct-auth-secret",
        "json-access-secret",
        "json-refresh-secret",
        "session-cookie-secret",
        "set-cookie-secret",
        "proxy-user",
        "proxy-password",
    )
    stderr = (
        b"Authorization = Basic basic-credential\n"
        b"Proxy-Authorization Bearer direct-auth-secret\n"
        b'{"access_token":"json-access-secret",'
        b'"refresh_token": "json-refresh-secret"}\n'
        b"Cookie: sid=session-cookie-secret; Path=/\n"
        b"Set-Cookie = auth=set-cookie-secret; Secure\n"
        b"proxy failed: https://proxy-user:proxy-password@proxy.example:8443\n"
    )
    collector, _ = _collector_with_fake_cli(
        monkeypatch,
        tmp_path,
        stdout=b"not json",
        stderr=stderr,
        returncode=1,
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        collector.collect(["NVDA.US"])

    rendered = str(error.value)
    assert rendered == "Longbridge quote command failed"
    for secret in secrets:
        assert secret not in rendered


def test_market_quote_pit_uses_available_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = json.dumps([_quote()]).encode()
    collector, _ = _collector_with_fake_cli(monkeypatch, tmp_path, stdout=response)
    market_evidence = collector.collect(["NVDA.US"]).evidence[0]
    document_evidence = load_evidence(ROOT / "examples/evidence.json")[0]
    request = ResearchRequest(
        request_id="pit-market-quote",
        question="在指定时点，行情快照是否已经可供研究系统使用？",
        universe=["NVDA.US"],
        as_of=market_evidence.known_at + timedelta(milliseconds=250),
        horizon="intraday",
        allowed_evidence_ids=[market_evidence.evidence_id, document_evidence.evidence_id],
    )

    eligible, rejected = filter_evidence_as_of(
        request, [document_evidence, market_evidence]
    )

    assert [item.evidence_id for item in eligible] == [document_evidence.evidence_id]
    assert [(item.evidence_id, item.reason) for item in rejected] == [
        (market_evidence.evidence_id, "available_at is after as_of")
    ]


def test_writer_refuses_to_overwrite_existing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = json.dumps([_quote()]).encode()
    collector, _ = _collector_with_fake_cli(monkeypatch, tmp_path, stdout=response)
    collection = collector.collect(["NVDA.US"])
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    sentinel = output_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_quote_collection(collection, output_dir)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_writer_refuses_to_replace_dangling_output_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = json.dumps([_quote()]).encode()
    collector, _ = _collector_with_fake_cli(monkeypatch, tmp_path, stdout=response)
    collection = collector.collect(["NVDA.US"])
    output = tmp_path / "dangling-output"
    target = tmp_path / "missing-target"
    output.symlink_to(target, target_is_directory=True)

    with pytest.raises(FileExistsError):
        write_quote_collection(collection, output)

    assert output.is_symlink()
    assert not target.exists()


def test_writer_revalidates_raw_hash_before_publishing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = json.dumps([_quote()]).encode()
    collector, _ = _collector_with_fake_cli(monkeypatch, tmp_path, stdout=response)
    collection = collector.collect(["NVDA.US"])

    with pytest.raises(ValueError, match="raw_sha256 mismatch"):
        write_quote_collection(
            replace(collection, raw_response=b"tampered"),
            tmp_path / "must-not-exist",
        )

    assert not (tmp_path / "must-not-exist").exists()


def test_writer_rejects_forged_raw_artifact_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = json.dumps([_quote()]).encode()
    collector, _ = _collector_with_fake_cli(monkeypatch, tmp_path, stdout=response)
    collection = collector.collect(["NVDA.US"])
    evidence = collection.evidence[0]
    assert evidence.provenance is not None
    forged = evidence.model_copy(
        update={
            "provenance": evidence.provenance.model_copy(
                update={"raw_artifact_ref": "elsewhere.json"}
            ),
            "record_sha256": "0" * 64,
        }
    )
    forged = forged.model_copy(
        update={"record_sha256": evidence_record_sha256(forged)}
    )

    with pytest.raises(ValueError, match="invalid raw artifact reference"):
        write_quote_collection(
            replace(collection, evidence=(forged,)),
            tmp_path / "must-not-exist",
        )

    assert not (tmp_path / "must-not-exist").exists()


def test_missing_binary_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(longbridge_module.shutil, "which", lambda _: None)

    with pytest.raises(LongbridgeCollectionError) as error:
        LongbridgeQuoteCollector().collect(["NVDA.US"])

    assert error.value.code == "binary_not_found"


def test_binary_disappearing_after_path_lookup_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        longbridge_module.shutil,
        "which",
        lambda _: "/definitely/missing/longbridge",
    )

    with pytest.raises(LongbridgeCollectionError) as error:
        LongbridgeQuoteCollector().collect(["NVDA.US"])

    assert error.value.code == "binary_not_found"


@pytest.mark.parametrize("timeout", [0.0, float("nan"), float("inf"), 121.0])
def test_timeout_configuration_is_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 120"):
        LongbridgeCollectorConfig(timeout_seconds=timeout)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_bounded_runner_stops_live_oversized_stream(stream: str) -> None:
    with tempfile.TemporaryDirectory() as runtime_dir:
        script = (
            "import sys, time\n"
            f"stream = sys.{stream}\n"
            "while True:\n"
            "    stream.write('x' * 64)\n"
            "    stream.flush()\n"
            "    time.sleep(0.001)\n"
        )
        started = time.monotonic()
        with pytest.raises(LongbridgeCollectionError) as oversized:
            longbridge_module._bounded_run(
                [sys.executable, "-c", script],
                cwd=runtime_dir,
                env=os.environ.copy(),
                timeout=2,
                stdout_limit=512,
                stderr_limit=512,
            )
        assert oversized.value.code == "output_too_large"
        assert time.monotonic() - started < 1.5


def test_bounded_runner_kills_timed_out_process() -> None:
    with tempfile.TemporaryDirectory() as runtime_dir:
        started = time.monotonic()
        with pytest.raises(LongbridgeCollectionError) as timed_out:
            longbridge_module._bounded_run(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; print('ready'); sys.stdout.flush(); time.sleep(5)",
                ],
                cwd=runtime_dir,
                env=os.environ.copy(),
                timeout=0.05,
                stdout_limit=100,
                stderr_limit=100,
            )
        assert timed_out.value.code == "timeout"
        assert time.monotonic() - started < 1.5


@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX-specific")
def test_bounded_runner_timeout_kills_descendants_holding_pipes() -> None:
    with tempfile.TemporaryDirectory() as runtime_dir:
        script = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n"
        )
        started = time.monotonic()
        with pytest.raises(LongbridgeCollectionError) as timed_out:
            longbridge_module._bounded_run(
                [sys.executable, "-c", script],
                cwd=runtime_dir,
                env=os.environ.copy(),
                timeout=0.05,
                stdout_limit=100,
                stderr_limit=100,
            )

        assert timed_out.value.code == "timeout"
        assert time.monotonic() - started < 1.5
