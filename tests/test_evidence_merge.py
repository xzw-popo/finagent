import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import finresearch.evidence_merge as merge_module
from finresearch import __version__
from finresearch.evidence import evidence_record_sha256, load_evidence
from finresearch.evidence_merge import (
    DEFAULT_MERGE_LIMITS,
    EvidenceMergeError,
    MergeLimits,
    merge_evidence_bundles,
)
from finresearch.schemas import Evidence, EvidenceBundle, EvidenceProvenance


def _with_record_hash(evidence: Evidence) -> Evidence:
    return evidence.model_copy(
        update={"record_sha256": evidence_record_sha256(evidence)}
    )


def _document(evidence_id: str, known_at: datetime) -> Evidence:
    excerpt = f"Audited document evidence for {evidence_id}."
    return _with_record_hash(
        Evidence(
            evidence_id=evidence_id,
            title=f"Document {evidence_id}",
            publisher="Example Publisher",
            uri="https://example.com/report",
            locator=f"section {evidence_id}",
            excerpt=excerpt,
            published_at=known_at - timedelta(hours=1),
            known_at=known_at,
            retrieved_at=known_at,
            content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            record_sha256="0" * 64,
        )
    )


def _market(
    evidence_id: str,
    symbol: str,
    raw_ref: str,
    raw: bytes,
    available_at: datetime,
) -> Evidence:
    excerpt = f"{symbol} normalized market quote snapshot at 180.00."
    content_sha256 = hashlib.sha256(excerpt.encode()).hexdigest()
    observed_at = available_at - timedelta(milliseconds=2)
    return _with_record_hash(
        Evidence(
            evidence_type="market_quote_snapshot",
            evidence_id=evidence_id,
            title=f"{symbol} quote snapshot",
            publisher="Longbridge Securities",
            uri="https://open.longbridge.com",
            locator=f"cli.quote {symbol}",
            excerpt=excerpt,
            published_at=None,
            known_at=available_at - timedelta(milliseconds=1),
            retrieved_at=available_at - timedelta(milliseconds=1),
            available_at=available_at,
            content_sha256=content_sha256,
            record_sha256="0" * 64,
            provenance=EvidenceProvenance(
                provider="longbridge",
                source_type="market_quote_snapshot",
                source_endpoint="cli.quote",
                symbol=symbol,
                observed_at=observed_at,
                freshness="unknown",
                raw_artifact_ref=raw_ref,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                normalized_sha256=content_sha256,
                normalizer_version="longbridge.quote.v1",
            ),
        )
    )


def _write_bundle(directory: Path, evidence: list[Evidence]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "evidence.json"
    path.write_text(
        EvidenceBundle(evidence=evidence).model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def test_merge_is_self_contained_content_addressed_and_does_not_filter_pit(
    tmp_path: Path,
) -> None:
    known_at = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    future_available_at = known_at + timedelta(days=7)
    document_path = _write_bundle(
        tmp_path / "documents", [_document("ev-z-document", known_at)]
    )
    raw = b'[{"symbol":"NVDA.US","last":"180.00"}]\n'
    market_dir = tmp_path / "quotes"
    (market_dir / "raw-response.json").parent.mkdir(parents=True)
    (market_dir / "raw-response.json").write_bytes(raw)
    market = _market(
        "ev-a-quote",
        "NVDA.US",
        "raw-response.json",
        raw,
        future_available_at,
    )
    input_record_hash = market.record_sha256
    market_path = _write_bundle(market_dir, [market])

    result = merge_evidence_bundles(
        [document_path, market_path],
        tmp_path / "merged",
        clock=lambda: datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    )

    assert result.evidence_ids == ("ev-a-quote", "ev-z-document")
    assert result.minimum_as_of_for_all_evidence == future_available_at
    assert result.input_count == 2
    assert result.evidence_count == 2
    assert result.artifact_count == 1
    merged = load_evidence(result.evidence_path)
    assert [item.evidence_id for item in merged] == ["ev-a-quote", "ev-z-document"]
    merged_market = merged[0]
    assert merged_market.available_at == future_available_at
    assert merged_market.provenance is not None
    digest = hashlib.sha256(raw).hexdigest()
    assert merged_market.provenance.raw_artifact_ref == f"artifacts/sha256/{digest}"
    assert (tmp_path / "merged" / "artifacts" / "sha256" / digest).read_bytes() == raw
    assert merged_market.record_sha256 != input_record_hash

    manifest_text = result.manifest_path.read_text()
    manifest = json.loads(manifest_text)
    assert manifest["tool_version"] == __version__
    assert manifest["pit_filter_applied"] is False
    assert manifest["minimum_as_of_for_all_evidence"] == "2026-09-08T08:00:00Z"
    assert manifest["records"][0] == {
        "evidence_id": "ev-a-quote",
        "input_ordinal": 2,
        "input_record_sha256": input_record_hash,
        "output_record_sha256": merged_market.record_sha256,
    }
    assert "raw-response.json" not in manifest_text
    assert str(document_path.resolve()) not in manifest_text
    assert str(market_path.resolve()) not in manifest_text
    assert stat.S_IMODE(result.evidence_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(
        (tmp_path / "merged" / "artifacts" / "sha256" / digest).stat().st_mode
    ) == 0o600


def test_duplicate_evidence_id_always_fails_before_output(tmp_path: Path) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    evidence = _document("ev-duplicate", known_at)
    first = _write_bundle(tmp_path / "first", [evidence])
    second = _write_bundle(tmp_path / "second", [evidence])
    output = tmp_path / "merged"

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([first, second], output)

    assert error.value.code == "duplicate_evidence_id"
    assert not output.exists()
    assert not list(tmp_path.glob(".finresearch-merge-*"))


def test_same_original_sidecar_path_with_different_hashes_does_not_collide(
    tmp_path: Path,
) -> None:
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    paths = []
    raw_values = [
        b'[{"symbol":"NVDA.US","last":"180.00"}]\n',
        b'[{"symbol":"AMD.US","last":"181.00"}]\n',
    ]
    for index, (symbol, raw) in enumerate(
        zip(("NVDA.US", "AMD.US"), raw_values, strict=True)
    ):
        directory = tmp_path / f"source-{index}"
        directory.mkdir()
        (directory / "raw-response.json").write_bytes(raw)
        paths.append(
            _write_bundle(
                directory,
                [
                    _market(
                        f"quote-{index}",
                        symbol,
                        "raw-response.json",
                        raw,
                        available_at,
                    )
                ],
            )
        )

    result = merge_evidence_bundles(paths, tmp_path / "merged")

    assert result.artifact_count == 2
    for raw in raw_values:
        digest = hashlib.sha256(raw).hexdigest()
        assert (tmp_path / "merged/artifacts/sha256" / digest).read_bytes() == raw


def test_different_original_paths_with_same_hash_share_one_blob(tmp_path: Path) -> None:
    raw = b'[{"symbol":"NVDA.US","last":"180.00"}]\n'
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    (first_dir / "raw").mkdir(parents=True)
    (second_dir / "provider").mkdir(parents=True)
    (first_dir / "raw/one.json").write_bytes(raw)
    (second_dir / "provider/two.data").write_bytes(raw)
    first = _write_bundle(
        first_dir,
        [_market("quote-one", "NVDA.US", "raw/one.json", raw, available_at)],
    )
    second = _write_bundle(
        second_dir,
        [_market("quote-two", "AMD.US", "provider/two.data", raw, available_at)],
    )

    result = merge_evidence_bundles([first, second], tmp_path / "merged")
    merged = load_evidence(result.evidence_path)

    assert result.artifact_count == 1
    assert {
        item.provenance.raw_artifact_ref
        for item in merged
        if item.provenance is not None
    } == {f"artifacts/sha256/{hashlib.sha256(raw).hexdigest()}"}


@pytest.mark.parametrize("payload", [b'{"evidence":[],"evidence":[]}\n', b'{"evidence":NaN}\n'])
def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(
    tmp_path: Path, payload: bytes
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(payload)
    valid = _write_bundle(
        tmp_path / "valid",
        [_document("ev-valid", datetime(2026, 9, 1, tzinfo=UTC))],
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([invalid, valid], tmp_path / "merged")

    assert error.value.code == "invalid_bundle"


def test_sidecar_symlink_is_rejected(tmp_path: Path) -> None:
    raw = b'[{"symbol":"NVDA.US","last":"180.00"}]\n'
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    market_dir = tmp_path / "market"
    market_dir.mkdir()
    (market_dir / "raw-response.json").symlink_to(outside)
    market = _write_bundle(
        market_dir,
        [
            _market(
                "ev-market",
                "NVDA.US",
                "raw-response.json",
                raw,
                datetime(2026, 9, 1, tzinfo=UTC),
            )
        ],
    )
    document = _write_bundle(
        tmp_path / "document",
        [_document("ev-document", datetime(2026, 9, 1, tzinfo=UTC))],
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([market, document], tmp_path / "merged")

    assert error.value.code == "invalid_bundle"
    assert "symlink" in str(error.value)


@pytest.mark.parametrize(
    "raw_ref",
    [
        "raw\\response.json",
        "raw/../response.json",
        "raw//response.json",
        "CON.json",
        "raw./response.json",
        "decomposed-e\u0301.json",
        "control-\x01.json",
        f"{'a' * 256}.json",
    ],
)
def test_nonportable_raw_references_fail_without_echoing_evidence_id(
    tmp_path: Path, raw_ref: str
) -> None:
    raw = b"provider response"
    untrusted_id = "secret-evidence-id\rCONTROL"
    market = _write_bundle(
        tmp_path / "market",
        [
            _market(
                untrusted_id,
                "NVDA.US",
                raw_ref,
                raw,
                datetime(2026, 9, 1, tzinfo=UTC),
            )
        ],
    )
    document = _write_bundle(
        tmp_path / "document",
        [_document("ev-document", datetime(2026, 9, 1, tzinfo=UTC))],
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([market, document], tmp_path / "merged")

    assert error.value.code == "invalid_bundle"
    assert untrusted_id not in str(error.value)


def test_existing_dangling_output_symlink_is_not_replaced(tmp_path: Path) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])
    output = tmp_path / "merged"
    output.symlink_to(tmp_path / "missing-target")

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([first, second], output)

    assert error.value.code == "output_error"
    assert output.is_symlink()


def test_publication_failure_retains_private_staging_for_safe_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])
    output = tmp_path / "merged"

    def fail_write(_parent_fd: int, _name: str, _data: bytes) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(merge_module, "_write_file_at_fsynced", fail_write)

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([first, second], output)

    assert error.value.code == "output_error"
    assert not output.exists()
    leftovers = list(tmp_path.glob(".finresearch-merge-*"))
    assert len(leftovers) == 1
    assert list(leftovers[0].iterdir()) == []
    assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o700


def test_merge_resource_limits(tmp_path: Path) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])

    with pytest.raises(EvidenceMergeError) as too_few:
        merge_evidence_bundles([first], tmp_path / "few")
    assert too_few.value.code == "input_error"

    with pytest.raises(EvidenceMergeError) as too_many:
        merge_evidence_bundles(
            [first, second],
            tmp_path / "many",
            limits=replace(DEFAULT_MERGE_LIMITS, max_inputs=1),
        )
    assert too_many.value.code == "resource_limit"

    with pytest.raises(EvidenceMergeError) as bundle_limit:
        merge_evidence_bundles(
            [first, second],
            tmp_path / "bundle-limit",
            limits=replace(DEFAULT_MERGE_LIMITS, max_bundle_bytes=1),
        )
    assert bundle_limit.value.code == "resource_limit"

    total_bundle_bytes = first.stat().st_size + second.stat().st_size
    with pytest.raises(EvidenceMergeError) as total_bundle_limit:
        merge_evidence_bundles(
            [first, second],
            tmp_path / "total-bundle-limit",
            limits=replace(
                DEFAULT_MERGE_LIMITS,
                max_total_bundle_bytes=total_bundle_bytes - 1,
            ),
        )
    assert total_bundle_limit.value.code == "resource_limit"

    with pytest.raises(EvidenceMergeError) as evidence_limit:
        merge_evidence_bundles(
            [first, second],
            tmp_path / "evidence-limit",
            limits=replace(DEFAULT_MERGE_LIMITS, max_evidence=1),
        )
    assert evidence_limit.value.code == "resource_limit"


def test_missing_no_follow_capability_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([], tmp_path / "merged")

    assert error.value.code == "output_error"
    assert "safe filesystem primitives" in str(error.value)
    assert not (tmp_path / "merged").exists()


def test_bundle_parent_resolution_error_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "loop" / "evidence.json"
    real_resolve = Path.resolve

    def fail_loop_resolution(path: Path, strict: bool = False) -> Path:
        if path == input_path.parent:
            raise RuntimeError("simulated symlink loop")
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_loop_resolution)

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles(
            [input_path, tmp_path / "other" / "evidence.json"],
            tmp_path / "merged",
        )

    assert error.value.code == "invalid_bundle"
    assert not (tmp_path / "merged").exists()


def test_artifact_count_single_and_total_byte_limits(tmp_path: Path) -> None:
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    paths: list[Path] = []
    raw_values = [b"first raw artifact", b"second raw artifact"]
    for index, raw in enumerate(raw_values):
        directory = tmp_path / f"source-{index}"
        directory.mkdir()
        (directory / "raw.bin").write_bytes(raw)
        paths.append(
            _write_bundle(
                directory,
                [
                    _market(
                        f"ev-{index}",
                        ("NVDA.US", "AMD.US")[index],
                        "raw.bin",
                        raw,
                        available_at,
                    )
                ],
            )
        )

    cases: list[tuple[str, MergeLimits]] = [
        (
            "artifact-count",
            replace(DEFAULT_MERGE_LIMITS, max_artifacts=1),
        ),
        (
            "single-bytes",
            replace(DEFAULT_MERGE_LIMITS, max_single_artifact_bytes=1),
        ),
        (
            "total-bytes",
            replace(
                DEFAULT_MERGE_LIMITS,
                max_total_artifact_bytes=len(raw_values[0]),
            ),
        ),
    ]
    for output_name, limits in cases:
        with pytest.raises(EvidenceMergeError) as error:
            merge_evidence_bundles(paths, tmp_path / output_name, limits=limits)
        assert error.value.code == "resource_limit"
        assert not (tmp_path / output_name).exists()


def test_merge_limits_require_positive_integers() -> None:
    with pytest.raises(ValueError, match="max_inputs"):
        MergeLimits(max_inputs=0)
    with pytest.raises(ValueError, match="max_bundle_bytes"):
        MergeLimits(max_bundle_bytes=True)


def test_clock_must_return_timezone_aware_datetime(tmp_path: Path) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles(
            [first, second],
            tmp_path / "merged",
            clock=lambda: "not-a-datetime",  # type: ignore[return-value]
        )

    assert error.value.code == "input_error"


def test_deeply_nested_json_is_a_classified_invalid_bundle(tmp_path: Path) -> None:
    invalid = tmp_path / "deep.json"
    invalid.write_text(
        '{"evidence":' + "[" * 20_000 + "0" + "]" * 20_000 + "}",
        encoding="utf-8",
    )
    valid = _write_bundle(
        tmp_path / "valid",
        [_document("ev-valid", datetime(2026, 9, 1, tzinfo=UTC))],
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([invalid, valid], tmp_path / "merged")

    assert error.value.code == "invalid_bundle"
    assert not (tmp_path / "merged").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_fifo_input_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "evidence.fifo"
    os.mkfifo(fifo)
    valid = _write_bundle(
        tmp_path / "valid",
        [_document("ev-valid", datetime(2026, 9, 1, tzinfo=UTC))],
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([fifo, valid], tmp_path / "merged")

    assert error.value.code == "invalid_bundle"


def test_native_publish_never_replaces_an_existing_empty_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "staging"
    destination = tmp_path / "merged"
    source.mkdir()
    destination.mkdir()

    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            merge_module._rename_directory_noreplace_at(
                parent_fd,
                source.name,
                destination.name,
                destination,
            )
    finally:
        os.close(parent_fd)

    assert source.is_dir()
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_native_publish_runtime_restriction_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])

    def reject_native_library(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated managed-runtime audit rejection")

    monkeypatch.setattr(merge_module.ctypes, "CDLL", reject_native_library)

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([first, second], tmp_path / "merged")

    assert error.value.code == "output_error"
    assert not (tmp_path / "merged").exists()


def test_post_commit_fsync_failure_returns_explicit_durability_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])
    output = tmp_path / "merged"
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    original_fsync = os.fsync

    def fail_after_commit(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        if (
            (descriptor_stat.st_dev, descriptor_stat.st_ino) == parent_identity
            and output.exists()
        ):
            raise OSError("simulated parent fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_commit)

    result = merge_evidence_bundles([first, second], output)

    assert result.durability_confirmed is False
    assert len(load_evidence(result.evidence_path)) == 2


def test_output_parent_path_replacement_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])
    parked = tmp_path.parent / f"{tmp_path.name}-parked"
    real_create_staging = merge_module._create_private_staging_at
    swapped = False

    def swap_parent_then_create(parent_fd: int) -> tuple[str, int]:
        nonlocal swapped
        tmp_path.rename(parked)
        tmp_path.mkdir()
        swapped = True
        return real_create_staging(parent_fd)

    monkeypatch.setattr(
        merge_module,
        "_create_private_staging_at",
        swap_parent_then_create,
    )
    try:
        with pytest.raises(EvidenceMergeError) as error:
            merge_evidence_bundles([first, second], tmp_path / "merged")

        assert error.value.code == "output_error"
        assert swapped is True
        assert not (tmp_path / "merged").exists()
        assert not (parked / "merged").exists()
    finally:
        if tmp_path.is_dir():
            tmp_path.rmdir()
        if parked.is_dir():
            parked.rename(tmp_path)


def test_total_artifact_limit_counts_distinct_sources_before_blob_deduplication(
    tmp_path: Path,
) -> None:
    raw = b"same raw"
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    inputs: list[Path] = []
    for index in range(2):
        directory = tmp_path / f"source-{index}"
        directory.mkdir()
        (directory / "raw.bin").write_bytes(raw)
        inputs.append(
            _write_bundle(
                directory,
                [
                    _market(
                        f"ev-{index}",
                        ("NVDA.US", "AMD.US")[index],
                        "raw.bin",
                        raw,
                        available_at,
                    )
                ],
            )
        )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles(
            inputs,
            tmp_path / "merged",
            limits=replace(
                DEFAULT_MERGE_LIMITS,
                max_total_artifact_bytes=len(raw),
            ),
        )

    assert error.value.code == "resource_limit"
    assert not (tmp_path / "merged").exists()


def test_intermediate_directory_swap_cannot_redirect_an_open_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_raw = b"SAFE-BUNDLE-BYTES"
    outside_raw = b"OUTSIDE-SECRET-BYTES"
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    market_dir = tmp_path / "market"
    nested = market_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "raw.bin").write_bytes(safe_raw)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "raw.bin").write_bytes(outside_raw)
    market = _write_bundle(
        market_dir,
        [
            _market(
                "ev-market",
                "NVDA.US",
                "nested/raw.bin",
                safe_raw,
                available_at,
            )
        ],
    )
    document = _write_bundle(
        tmp_path / "document",
        [_document("ev-document", available_at)],
    )
    real_open = os.open
    swapped = False

    def swap_during_final_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "raw.bin" and dir_fd is not None and not swapped:
            detached = market_dir / "detached"
            nested.rename(detached)
            nested.symlink_to(outside_dir, target_is_directory=True)
            try:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                nested.unlink()
                detached.rename(nested)
            swapped = True
            return descriptor
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_during_final_open)

    merge_evidence_bundles([market, document], tmp_path / "merged")

    assert swapped is True
    digest = hashlib.sha256(safe_raw).hexdigest()
    assert (tmp_path / "merged/artifacts/sha256" / digest).read_bytes() == safe_raw
    assert outside_raw not in (tmp_path / "merged/artifacts/sha256" / digest).read_bytes()


def test_bundle_root_swap_cannot_change_the_sidecar_bound_to_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_raw = b"ORIGINAL-BUNDLE-BYTES"
    replacement_raw = b"REPLACEMENT-ROOT-SECRET"
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    market_dir = tmp_path / "market"
    market_dir.mkdir()
    (market_dir / "raw.bin").write_bytes(safe_raw)
    market = _write_bundle(
        market_dir,
        [_market("ev-market", "NVDA.US", "raw.bin", safe_raw, available_at)],
    )
    document = _write_bundle(
        tmp_path / "document",
        [_document("ev-document", available_at)],
    )
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "raw.bin").write_bytes(replacement_raw)
    real_open_sidecar = merge_module._open_sidecar
    swapped = False

    def swap_root(source: object) -> int:
        nonlocal swapped
        if not swapped:
            parked = tmp_path / "parked"
            market_dir.rename(parked)
            replacement.rename(market_dir)
            try:
                descriptor = real_open_sidecar(source)  # type: ignore[arg-type]
            finally:
                market_dir.rename(replacement)
                parked.rename(market_dir)
            swapped = True
            return descriptor
        return real_open_sidecar(source)  # type: ignore[arg-type]

    monkeypatch.setattr(merge_module, "_open_sidecar", swap_root)

    merge_evidence_bundles([market, document], tmp_path / "merged")

    assert swapped is True
    digest = hashlib.sha256(safe_raw).hexdigest()
    assert (tmp_path / "merged/artifacts/sha256" / digest).read_bytes() == safe_raw


def test_staging_path_replacement_is_never_deleted_as_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])
    replaced = False

    def replace_staging_then_fail(_parent_fd: int, _name: str, _data: bytes) -> None:
        nonlocal replaced
        staging = next(tmp_path.glob(".finresearch-merge-*"))
        staging.rename(tmp_path / "parked-staging")
        staging.mkdir()
        (staging / "victim-marker").write_text("DO-NOT-DELETE", encoding="utf-8")
        replaced = True
        raise OSError("simulated output failure")

    monkeypatch.setattr(
        merge_module,
        "_write_file_at_fsynced",
        replace_staging_then_fail,
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([first, second], tmp_path / "merged")

    assert error.value.code == "output_error"
    assert replaced is True
    replacement = next(tmp_path.glob(".finresearch-merge-*"))
    assert (replacement / "victim-marker").read_text() == "DO-NOT-DELETE"


def test_failed_staging_does_not_delete_concurrently_inserted_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])
    inserted_marker: Path | None = None

    def insert_child_then_fail(_parent_fd: int, _name: str, _data: bytes) -> None:
        nonlocal inserted_marker
        staging = next(tmp_path.glob(".finresearch-merge-*"))
        inserted = staging / "concurrently-inserted"
        inserted.mkdir()
        inserted_marker = inserted / "do-not-delete"
        inserted_marker.write_text("UNRELATED", encoding="utf-8")
        raise OSError("simulated output failure")

    monkeypatch.setattr(
        merge_module,
        "_write_file_at_fsynced",
        insert_child_then_fail,
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([first, second], tmp_path / "merged")

    assert error.value.code == "output_error"
    assert inserted_marker is not None
    assert inserted_marker.read_text(encoding="utf-8") == "UNRELATED"


def test_staging_subtree_change_before_commit_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"provider response"
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    market_dir = tmp_path / "market"
    market_dir.mkdir()
    (market_dir / "raw.bin").write_bytes(raw)
    market = _write_bundle(
        market_dir,
        [_market("ev-market", "NVDA.US", "raw.bin", raw, available_at)],
    )
    document = _write_bundle(
        tmp_path / "document",
        [_document("ev-document", available_at)],
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("DO-NOT-TOUCH", encoding="utf-8")
    real_validate = merge_module._validate_staged_output
    validation_count = 0

    def mutate_after_first_validation(*args: object, **kwargs: object) -> None:
        nonlocal validation_count
        real_validate(*args, **kwargs)  # type: ignore[arg-type]
        validation_count += 1
        if validation_count == 1:
            staging = next(tmp_path.glob(".finresearch-merge-*"))
            sha_dir = staging / "artifacts" / "sha256"
            sha_dir.rename(staging / "artifacts" / "sha256-parked")
            sha_dir.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        merge_module,
        "_validate_staged_output",
        mutate_after_first_validation,
    )

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([market, document], tmp_path / "merged")

    assert error.value.code == "output_error"
    assert not (tmp_path / "merged").exists()
    assert marker.read_text(encoding="utf-8") == "DO-NOT-TOUCH"


def test_bundle_read_io_error_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_at = datetime(2026, 9, 1, tzinfo=UTC)
    first = _write_bundle(tmp_path / "first", [_document("ev-one", known_at)])
    second = _write_bundle(tmp_path / "second", [_document("ev-two", known_at)])
    monkeypatch.setattr(os, "read", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([first, second], tmp_path / "merged")

    assert error.value.code == "invalid_bundle"


def test_sidecar_read_io_error_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"provider response"
    available_at = datetime(2026, 9, 1, tzinfo=UTC)
    market_dir = tmp_path / "market"
    market_dir.mkdir()
    raw_path = market_dir / "raw.bin"
    raw_path.write_bytes(raw)
    market = _write_bundle(
        market_dir,
        [_market("ev-market", "NVDA.US", "raw.bin", raw, available_at)],
    )
    document = _write_bundle(
        tmp_path / "document",
        [_document("ev-document", available_at)],
    )
    raw_inode = raw_path.stat().st_ino
    real_read = os.read

    def fail_raw_read(descriptor: int, size: int) -> bytes:
        if os.fstat(descriptor).st_ino == raw_inode:
            raise OSError("simulated raw read failure")
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", fail_raw_read)

    with pytest.raises(EvidenceMergeError) as error:
        merge_evidence_bundles([market, document], tmp_path / "merged")

    assert error.value.code == "invalid_bundle"
