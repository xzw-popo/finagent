from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from finresearch import __version__
from finresearch.evidence import (
    evidence_record_sha256,
    validate_evidence_records,
)
from finresearch.schemas import Evidence, EvidenceBundle

_READ_CHUNK_SIZE = 1024 * 1024
_ZERO_SHA256 = "0" * 64
_MAX_RAW_REF_BYTES = 1024
_MAX_RAW_REF_PARTS = 16
_MAX_RAW_REF_PART_BYTES = 255
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*')
_HAS_REQUIRED_DIR_FD_SUPPORT = all(
    function in os.supports_dir_fd
    for function in (os.open, os.mkdir, os.stat)
)
_HAS_REQUIRED_FD_SUPPORT = all(
    function in os.supports_fd for function in (os.listdir, os.stat)
)
_HAS_REQUIRED_NOFOLLOW_STAT = os.stat in os.supports_follow_symlinks

MergeErrorCode = Literal[
    "output_error",
    "invalid_bundle",
    "input_error",
    "duplicate_evidence_id",
    "resource_limit",
]


class EvidenceMergeError(Exception):
    """Expected, user-facing failure raised by the evidence merge boundary."""

    def __init__(self, code: MergeErrorCode, message: str) -> None:
        self.code = code
        self.category = code
        super().__init__(message)


@dataclass(frozen=True)
class MergeLimits:
    max_inputs: int = 32
    max_bundle_bytes: int = 16 * 1024 * 1024
    max_total_bundle_bytes: int = 64 * 1024 * 1024
    max_json_depth: int = 128
    max_json_structural_tokens: int = 500_000
    max_evidence: int = 10_000
    max_artifacts: int = 10_000
    max_single_artifact_bytes: int = 256 * 1024 * 1024
    max_total_artifact_bytes: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_MERGE_LIMITS = MergeLimits()


@dataclass(frozen=True)
class MergeResult:
    evidence_path: Path
    manifest_path: Path
    evidence_ids: tuple[str, ...]
    minimum_as_of_for_all_evidence: datetime
    input_count: int
    evidence_count: int
    artifact_count: int
    durability_confirmed: bool = True


@dataclass(frozen=True)
class _LoadedInput:
    ordinal: int
    bundle_sha256: str
    source_dir_fd: int
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class _ArtifactSource:
    input_ordinal: int
    source_dir_fd: int
    raw_artifact_ref: str
    evidence_id: str


@dataclass
class _ArtifactPlan:
    raw_sha256: str
    size: int
    source: _ArtifactSource
    evidence_ids: set[str] = field(default_factory=set)


class _DuplicateJsonKey(ValueError):
    pass


def _require_safe_filesystem_capabilities() -> None:
    """Fail closed instead of silently dropping anti-traversal guarantees."""

    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if (
        any(not hasattr(os, name) for name in required_flags)
        or not _HAS_REQUIRED_DIR_FD_SUPPORT
        or not _HAS_REQUIRED_FD_SUPPORT
        or not _HAS_REQUIRED_NOFOLLOW_STAT
    ):
        raise EvidenceMergeError(
            "output_error",
            "safe filesystem primitives are unavailable on this platform",
        )


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _open_regular_file(
    path: str | Path,
    *,
    code: MergeErrorCode,
    label: str,
    dir_fd: int | None = None,
) -> int:
    # O_NONBLOCK prevents a FIFO or device masquerading as an input from
    # blocking in open() before the fstat regular-file check can reject it.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        descriptor = (
            os.open(path, flags)
            if dir_fd is None
            else os.open(path, flags, dir_fd=dir_fd)
        )
    except (OSError, ValueError, NotImplementedError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ELOOP:
            raise EvidenceMergeError(code, f"{label} symlink rejected") from exc
        raise EvidenceMergeError(code, f"unable to open {label}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EvidenceMergeError(code, f"{label} must be a regular file")
    except EvidenceMergeError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise EvidenceMergeError(code, f"unable to inspect {label}") from exc
    return descriptor


def _bounded_bundle_bytes(path: str, limit: int, *, source_dir_fd: int) -> bytes:
    descriptor = _open_regular_file(
        path,
        code="invalid_bundle",
        label="input bundle",
        dir_fd=source_dir_fd,
    )
    try:
        try:
            before = os.fstat(descriptor)
        except OSError as exc:
            raise EvidenceMergeError("invalid_bundle", "unable to read input bundle") from exc
        if before.st_size > limit:
            raise EvidenceMergeError("resource_limit", "input bundle exceeds max_bundle_bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(
                    descriptor,
                    min(_READ_CHUNK_SIZE, limit + 1 - total),
                )
            except OSError as exc:
                raise EvidenceMergeError(
                    "invalid_bundle", "unable to read input bundle"
                ) from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise EvidenceMergeError(
                    "resource_limit", "input bundle exceeds max_bundle_bytes"
                )
        try:
            after = os.fstat(descriptor)
        except OSError as exc:
            raise EvidenceMergeError("invalid_bundle", "unable to read input bundle") from exc
        if _stat_identity(before) != _stat_identity(after):
            raise EvidenceMergeError("invalid_bundle", "input bundle changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_bundle_root(path: Path) -> int:
    try:
        source_dir = path.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceMergeError("invalid_bundle", "input bundle does not exist") from exc

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(source_dir, flags)
    except OSError as exc:
        raise EvidenceMergeError("invalid_bundle", "input bundle root is unavailable") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise EvidenceMergeError("invalid_bundle", "input bundle root is invalid")
    except EvidenceMergeError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise EvidenceMergeError("invalid_bundle", "input bundle root is unavailable") from exc
    return descriptor


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _enforce_json_complexity(text: str, limits: MergeLimits) -> None:
    depth = 0
    structural_tokens = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            structural_tokens += 1
            if depth > limits.max_json_depth:
                raise EvidenceMergeError("invalid_bundle", "input JSON nesting is too deep")
        elif character in "]}":
            depth -= 1
        elif character == ",":
            structural_tokens += 1
        if structural_tokens > limits.max_json_structural_tokens:
            raise EvidenceMergeError(
                "resource_limit", "input JSON structure exceeds resource limits"
            )


def _parse_bundle(data: bytes, limits: MergeLimits) -> tuple[Evidence, ...]:
    try:
        text = data.decode("utf-8")
        _enforce_json_complexity(text, limits)
        payload = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
        bundle = EvidenceBundle.model_validate(payload)
        # Validate records individually so duplicate IDs receive their dedicated
        # cross-input error instead of the generic list validator error.
        return tuple(validate_evidence_records([item])[0] for item in bundle.evidence)
    except EvidenceMergeError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        raise EvidenceMergeError("invalid_bundle", "input is not a valid evidence bundle") from exc


def _load_inputs(
    paths: Sequence[Path], limits: MergeLimits, descriptors: ExitStack
) -> tuple[list[_LoadedInput], dict[str, int]]:
    if len(paths) < 2:
        raise EvidenceMergeError("input_error", "at least two evidence bundles are required")
    if len(paths) > limits.max_inputs:
        raise EvidenceMergeError("resource_limit", "input count exceeds max_inputs")

    loaded: list[_LoadedInput] = []
    evidence_locations: dict[str, int] = {}
    evidence_count = 0
    total_bundle_bytes = 0
    for ordinal, raw_path in enumerate(paths, start=1):
        path = Path(raw_path)
        source_dir_fd = _open_bundle_root(path)
        descriptors.callback(os.close, source_dir_fd)
        data = _bounded_bundle_bytes(
            path.name,
            limits.max_bundle_bytes,
            source_dir_fd=source_dir_fd,
        )
        total_bundle_bytes += len(data)
        if total_bundle_bytes > limits.max_total_bundle_bytes:
            raise EvidenceMergeError(
                "resource_limit", "input bytes exceed max_total_bundle_bytes"
            )
        evidence = _parse_bundle(data, limits)
        evidence_count += len(evidence)
        if evidence_count > limits.max_evidence:
            raise EvidenceMergeError("resource_limit", "evidence count exceeds max_evidence")
        for item in evidence:
            previous = evidence_locations.get(item.evidence_id)
            if previous is not None:
                raise EvidenceMergeError(
                    "duplicate_evidence_id",
                    f"duplicate evidence_id appears in inputs {previous} and {ordinal}",
                )
            evidence_locations[item.evidence_id] = ordinal
        loaded.append(
            _LoadedInput(
                ordinal=ordinal,
                bundle_sha256=hashlib.sha256(data).hexdigest(),
                source_dir_fd=source_dir_fd,
                evidence=evidence,
            )
        )
    return loaded, evidence_locations


def _validated_sidecar_parts(source: _ArtifactSource) -> tuple[str, ...]:
    raw_reference = source.raw_artifact_ref
    raw_parts = raw_reference.split("/")
    reference = Path(raw_reference)
    windows_reference = PureWindowsPath(raw_reference)
    if (
        len(raw_reference.encode("utf-8")) > _MAX_RAW_REF_BYTES
        or len(raw_parts) > _MAX_RAW_REF_PARTS
        or any(not part or part in {".", ".."} for part in raw_parts)
        or any(len(part.encode("utf-8")) > _MAX_RAW_REF_PART_BYTES for part in raw_parts)
        or "\\" in raw_reference
        or unicodedata.normalize("NFC", raw_reference) != raw_reference
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_reference)
        or any(character in _WINDOWS_FORBIDDEN_CHARS for character in raw_reference)
        or any(part.endswith((".", " ")) for part in raw_parts)
        or any(part.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES for part in raw_parts)
        or not reference.parts
        or reference.is_absolute()
        or windows_reference.is_absolute()
        or bool(windows_reference.drive)
        or ".." in reference.parts
        or ".." in windows_reference.parts
    ):
        raise EvidenceMergeError("invalid_bundle", "unsafe raw artifact reference")

    return tuple(raw_parts)


def _open_sidecar(source: _ArtifactSource) -> int:
    """Open a sidecar through pinned directory descriptors.

    Every component after the bundle root is traversed with openat semantics and
    O_NOFOLLOW. Holding each directory descriptor closes the check/open race in
    which an intermediate directory could otherwise be replaced by a symlink.
    """

    parts = _validated_sidecar_parts(source)
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW

    try:
        current_fd = os.dup(source.source_dir_fd)
    except OSError as exc:
        raise EvidenceMergeError(
            "invalid_bundle", "evidence source directory is unavailable"
        ) from exc

    try:
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise EvidenceMergeError(
                "invalid_bundle", "evidence source directory is unavailable"
            )
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    directory_flags,
                    dir_fd=current_fd,
                )
            except (OSError, NotImplementedError) as exc:
                raise EvidenceMergeError(
                    "invalid_bundle", "raw artifact path is invalid"
                ) from exc
            try:
                next_stat = os.fstat(next_fd)
            except OSError as exc:
                os.close(next_fd)
                raise EvidenceMergeError(
                    "invalid_bundle", "raw artifact path is invalid"
                ) from exc
            if not stat.S_ISDIR(next_stat.st_mode):
                os.close(next_fd)
                raise EvidenceMergeError(
                    "invalid_bundle", "raw artifact path is invalid"
                )
            os.close(current_fd)
            current_fd = next_fd
        try:
            return _open_regular_file(
                parts[-1],
                code="invalid_bundle",
                label="raw artifact",
                dir_fd=current_fd,
            )
        except (OSError, NotImplementedError) as exc:
            raise EvidenceMergeError("invalid_bundle", "raw artifact is missing") from exc
    except EvidenceMergeError:
        raise
    except (OSError, NotImplementedError) as exc:
        raise EvidenceMergeError("invalid_bundle", "raw artifact path is invalid") from exc
    finally:
        os.close(current_fd)


def _hash_sidecar(source: _ArtifactSource, limit: int) -> tuple[str, int]:
    descriptor = _open_sidecar(source)
    try:
        try:
            before = os.fstat(descriptor)
        except OSError as exc:
            raise EvidenceMergeError("invalid_bundle", "unable to read raw artifact") from exc
        if before.st_size > limit:
            raise EvidenceMergeError(
                "resource_limit", "raw artifact exceeds allowed byte limit"
            )
        hasher = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, _READ_CHUNK_SIZE)
            except OSError as exc:
                raise EvidenceMergeError(
                    "invalid_bundle", "unable to read raw artifact"
                ) from exc
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise EvidenceMergeError(
                    "resource_limit", "raw artifact exceeds allowed byte limit"
                )
            hasher.update(chunk)
        try:
            after = os.fstat(descriptor)
        except OSError as exc:
            raise EvidenceMergeError("invalid_bundle", "unable to read raw artifact") from exc
        if _stat_identity(before) != _stat_identity(after):
            raise EvidenceMergeError("invalid_bundle", "raw artifact changed while hashing")
        return hasher.hexdigest(), total
    finally:
        os.close(descriptor)


def _canonicalize_evidence(
    inputs: list[_LoadedInput], limits: MergeLimits
) -> tuple[
    list[Evidence],
    list[dict[str, Any]],
    dict[str, _ArtifactPlan],
    dict[int, set[str]],
]:
    transformed: list[Evidence] = []
    record_manifest: list[dict[str, Any]] = []
    artifacts: dict[str, _ArtifactPlan] = {}
    input_artifacts = {item.ordinal: set() for item in inputs}
    verified_sources: dict[tuple[int, str], tuple[str, int]] = {}
    total_scanned_artifact_bytes = 0

    for loaded in inputs:
        for item in loaded.evidence:
            output_item = item
            if item.provenance is not None:
                source = _ArtifactSource(
                    input_ordinal=loaded.ordinal,
                    source_dir_fd=loaded.source_dir_fd,
                    raw_artifact_ref=item.provenance.raw_artifact_ref,
                    evidence_id=item.evidence_id,
                )
                source_key = (source.input_ordinal, source.raw_artifact_ref)
                verified = verified_sources.get(source_key)
                if verified is None:
                    remaining_artifact_bytes = (
                        limits.max_total_artifact_bytes
                        - total_scanned_artifact_bytes
                    )
                    if remaining_artifact_bytes <= 0:
                        raise EvidenceMergeError(
                            "resource_limit",
                            "artifact scan exceeds max_total_artifact_bytes",
                        )
                    actual_sha256, size = _hash_sidecar(
                        source,
                        min(
                            limits.max_single_artifact_bytes,
                            remaining_artifact_bytes,
                        ),
                    )
                    total_scanned_artifact_bytes += size
                    verified = (actual_sha256, size)
                    verified_sources[source_key] = verified
                actual_sha256, size = verified
                if actual_sha256 != item.provenance.raw_sha256:
                    raise EvidenceMergeError("invalid_bundle", "raw_sha256 mismatch")

                raw_sha256 = item.provenance.raw_sha256
                plan = artifacts.get(raw_sha256)
                if plan is None:
                    if len(artifacts) >= limits.max_artifacts:
                        raise EvidenceMergeError(
                            "resource_limit", "artifact count exceeds max_artifacts"
                        )
                    plan = _ArtifactPlan(
                        raw_sha256=raw_sha256,
                        size=size,
                        source=source,
                    )
                    artifacts[raw_sha256] = plan
                elif plan.size != size:
                    raise EvidenceMergeError(
                        "invalid_bundle", "inconsistent files claim the same raw_sha256"
                    )
                plan.evidence_ids.add(item.evidence_id)
                input_artifacts[loaded.ordinal].add(raw_sha256)

                canonical_ref = f"artifacts/sha256/{raw_sha256}"
                provenance = item.provenance.model_copy(
                    update={"raw_artifact_ref": canonical_ref}
                )
                unhashed = item.model_copy(
                    update={"provenance": provenance, "record_sha256": _ZERO_SHA256}
                )
                output_item = unhashed.model_copy(
                    update={"record_sha256": evidence_record_sha256(unhashed)}
                )

            transformed.append(output_item)
            record_manifest.append(
                {
                    "evidence_id": item.evidence_id,
                    "input_ordinal": loaded.ordinal,
                    "input_record_sha256": item.record_sha256,
                    "output_record_sha256": output_item.record_sha256,
                }
            )

    transformed.sort(key=lambda item: item.evidence_id)
    record_manifest.sort(key=lambda item: item["evidence_id"])
    try:
        transformed = validate_evidence_records(transformed)
    except ValueError as exc:
        raise EvidenceMergeError("invalid_bundle", "canonical evidence validation failed") from exc
    return transformed, record_manifest, artifacts, input_artifacts


def _copy_artifact(
    plan: _ArtifactPlan,
    destination_dir_fd: int,
    destination_name: str,
    limit: int,
) -> None:
    source_descriptor = _open_sidecar(plan.source)
    try:
        try:
            before = os.fstat(source_descriptor)
        except OSError as exc:
            raise EvidenceMergeError("invalid_bundle", "unable to read raw artifact") from exc
        if before.st_size > limit or before.st_size != plan.size:
            raise EvidenceMergeError("invalid_bundle", "raw artifact changed before copying")
        hasher = hashlib.sha256()
        total = 0
        output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        output_flags |= os.O_CLOEXEC | os.O_NOFOLLOW
        output_descriptor = os.open(
            destination_name,
            output_flags,
            0o600,
            dir_fd=destination_dir_fd,
        )
        with os.fdopen(output_descriptor, "wb") as output:
            while True:
                try:
                    chunk = os.read(source_descriptor, _READ_CHUNK_SIZE)
                except OSError as exc:
                    raise EvidenceMergeError(
                        "invalid_bundle", "unable to read raw artifact"
                    ) from exc
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise EvidenceMergeError(
                        "resource_limit", "raw artifact exceeds max_single_artifact_bytes"
                    )
                hasher.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        try:
            after = os.fstat(source_descriptor)
        except OSError as exc:
            raise EvidenceMergeError("invalid_bundle", "unable to read raw artifact") from exc
        if _stat_identity(before) != _stat_identity(after):
            raise EvidenceMergeError("invalid_bundle", "raw artifact changed while copying")
        if total != plan.size or hasher.hexdigest() != plan.raw_sha256:
            raise EvidenceMergeError("invalid_bundle", "raw artifact changed before publication")
    finally:
        os.close(source_descriptor)


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.ENOTDIR, "staging component is not a directory")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _create_directory_at(parent_fd: int, name: str) -> int:
    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    return _open_directory_at(parent_fd, name)


def _write_file_at_fsynced(parent_fd: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _read_file_at(parent_fd: int, name: str, expected_bytes: int) -> bytes:
    descriptor = _open_regular_file(
        name,
        code="output_error",
        label="staged output",
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        if before.st_size != expected_bytes:
            raise EvidenceMergeError("output_error", "staged output size mismatch")
        chunks: list[bytes] = []
        total = 0
        while total <= expected_bytes:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_SIZE, expected_bytes + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if total != expected_bytes or _stat_identity(before) != _stat_identity(after):
            raise EvidenceMergeError("output_error", "staged output changed during validation")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _hash_file_at(parent_fd: int, name: str, expected_size: int) -> str:
    descriptor = _open_regular_file(
        name,
        code="output_error",
        label="staged artifact",
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        if before.st_size != expected_size:
            raise EvidenceMergeError("output_error", "staged artifact size mismatch")
        hasher = hashlib.sha256()
        total = 0
        while total <= expected_size:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_SIZE, expected_size + 1 - total),
            )
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if total != expected_size or _stat_identity(before) != _stat_identity(after):
            raise EvidenceMergeError("output_error", "staged artifact changed during validation")
        return hasher.hexdigest()
    finally:
        os.close(descriptor)


def _same_file_identity(left_fd: int, right_fd: int) -> bool:
    left = os.fstat(left_fd)
    right = os.fstat(right_fd)
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _directory_entry_matches_fd(parent_fd: int, name: str, root_fd: int) -> bool:
    try:
        current_fd = _open_directory_at(parent_fd, name)
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        return _same_file_identity(current_fd, root_fd)
    finally:
        os.close(current_fd)


def _directory_path_matches_fd(path: Path, directory_fd: int) -> bool:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current_fd = os.open(path, flags)
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        return _same_file_identity(current_fd, directory_fd)
    finally:
        os.close(current_fd)


def _entry_exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise EvidenceMergeError("output_error", "unable to inspect output path") from exc
    return True


def _open_output_parent(output_dir: Path) -> tuple[Path, int]:
    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        parent_path = output_dir.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceMergeError("output_error", "output parent is unavailable") from exc

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent_fd = os.open(parent_path, flags)
    except (OSError, ValueError) as exc:
        raise EvidenceMergeError("output_error", "output parent is unavailable") from exc
    try:
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            raise EvidenceMergeError("output_error", "output parent is unavailable")
    except Exception:
        os.close(parent_fd)
        raise
    return parent_path, parent_fd


def _create_private_staging_at(parent_fd: int) -> tuple[str, int]:
    for _ in range(128):
        try:
            name = f".finresearch-merge-{secrets.token_hex(16)}"
        except Exception as exc:
            raise EvidenceMergeError(
                "output_error", "unable to allocate private staging directory"
            ) from exc
        try:
            return name, _create_directory_at(parent_fd, name)
        except FileExistsError:
            continue
    raise EvidenceMergeError("output_error", "unable to allocate private staging directory")


def _validate_staged_output(
    root_fd: int,
    artifacts_fd: int | None,
    sha256_fd: int | None,
    evidence_bytes: bytes,
    manifest_bytes: bytes,
    evidence: list[Evidence],
    artifacts: dict[str, _ArtifactPlan],
) -> None:
    expected_root = {"evidence.json", "merge.json"}
    if artifacts:
        expected_root.add("artifacts")
    if set(os.listdir(root_fd)) != expected_root:
        raise EvidenceMergeError("output_error", "staged output contains unexpected entries")

    staged_evidence = _read_file_at(root_fd, "evidence.json", len(evidence_bytes))
    staged_manifest = _read_file_at(root_fd, "merge.json", len(manifest_bytes))
    if staged_evidence != evidence_bytes or staged_manifest != manifest_bytes:
        raise EvidenceMergeError("output_error", "staged output content mismatch")
    staged_bundle = EvidenceBundle.model_validate_json(staged_evidence)
    if validate_evidence_records(staged_bundle.evidence) != evidence:
        raise EvidenceMergeError("output_error", "staged evidence validation mismatch")

    if not artifacts:
        return
    if artifacts_fd is None or sha256_fd is None:
        raise EvidenceMergeError("output_error", "staged artifact directory is missing")
    fresh_artifacts_fd = _open_directory_at(root_fd, "artifacts")
    try:
        if not _same_file_identity(artifacts_fd, fresh_artifacts_fd):
            raise EvidenceMergeError("output_error", "staged artifact directory changed")
        if set(os.listdir(fresh_artifacts_fd)) != {"sha256"}:
            raise EvidenceMergeError("output_error", "staged artifact layout is invalid")
        fresh_sha256_fd = _open_directory_at(fresh_artifacts_fd, "sha256")
        try:
            if not _same_file_identity(sha256_fd, fresh_sha256_fd):
                raise EvidenceMergeError("output_error", "staged hash directory changed")
            if set(os.listdir(fresh_sha256_fd)) != set(artifacts):
                raise EvidenceMergeError("output_error", "staged artifact set mismatch")
            for digest, plan in artifacts.items():
                if _hash_file_at(fresh_sha256_fd, digest, plan.size) != digest:
                    raise EvidenceMergeError("output_error", "staged artifact hash mismatch")
        finally:
            os.close(fresh_sha256_fd)
    finally:
        os.close(fresh_artifacts_fd)


def _raise_rename_error(error_number: int, destination: Path) -> None:
    raise OSError(error_number, os.strerror(error_number), destination)


def _rename_directory_noreplace_at(
    parent_fd: int,
    source_name: str,
    destination_name: str,
    destination: Path,
) -> None:
    """Atomically publish a directory without replacing any existing path.

    Python's POSIX ``os.rename`` may replace an empty destination directory, so
    the merge boundary uses the platform's native exclusive-rename primitive
    and fails closed when none is available.
    """

    try:
        if sys.platform == "darwin":
            library = ctypes.CDLL(None, use_errno=True)
            rename_exclusive = getattr(library, "renameatx_np", None)
            if rename_exclusive is None:
                _raise_rename_error(errno.ENOTSUP, destination)
            rename_exclusive.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename_exclusive.restype = ctypes.c_int
            # RENAME_EXCL from Darwin's stdio.h: fail if destination exists.
            if rename_exclusive(
                parent_fd,
                os.fsencode(source_name),
                parent_fd,
                os.fsencode(destination_name),
                0x00000004,
            ):
                _raise_rename_error(ctypes.get_errno(), destination)
            return

        if sys.platform.startswith("linux"):
            library = ctypes.CDLL(None, use_errno=True)
            rename_noreplace = getattr(library, "renameat2", None)
            if rename_noreplace is None:
                _raise_rename_error(errno.ENOTSUP, destination)
            rename_noreplace.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename_noreplace.restype = ctypes.c_int
            # RENAME_NOREPLACE=1 from Linux renameat2(2).
            if rename_noreplace(
                parent_fd,
                os.fsencode(source_name),
                parent_fd,
                os.fsencode(destination_name),
                1,
            ):
                _raise_rename_error(ctypes.get_errno(), destination)
            return

        _raise_rename_error(errno.ENOTSUP, destination)
    except OSError:
        raise
    except Exception as exc:
        # Managed runtimes may reject ctypes/dlopen via an audit hook. Keep
        # that operational limitation inside the stable output_error boundary.
        raise OSError(
            errno.ENOTSUP,
            "native no-replace publication is unavailable",
            destination,
        ) from exc


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _merge_loaded_inputs(
    loaded: list[_LoadedInput],
    output_dir: Path,
    *,
    effective_limits: MergeLimits,
    clock: Callable[[], datetime] = _now_utc,
) -> MergeResult:
    """Build and publish already loaded inputs while their root fds stay pinned."""

    output_dir = Path(output_dir)
    if os.path.lexists(output_dir):
        raise EvidenceMergeError("output_error", "output path already exists")

    transformed, records, artifacts, input_artifacts = _canonicalize_evidence(
        loaded, effective_limits
    )
    minimum_as_of = max(item.available_at or item.known_at for item in transformed)
    try:
        merged_at = clock()
    except Exception as exc:
        raise EvidenceMergeError("input_error", "merge clock failed") from exc
    if (
        not isinstance(merged_at, datetime)
        or merged_at.tzinfo is None
        or merged_at.utcoffset() is None
    ):
        raise EvidenceMergeError("input_error", "merge clock must return a timezone-aware value")

    evidence_payload = EvidenceBundle(evidence=transformed).model_dump(mode="json")
    evidence_bytes = _manifest_bytes(evidence_payload)
    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    artifact_manifest = [
        {
            "bytes": plan.size,
            "evidence_ids": sorted(plan.evidence_ids),
            "path": f"artifacts/sha256/{digest}",
            "sha256": digest,
        }
        for digest, plan in sorted(artifacts.items())
    ]
    manifest = {
        "schema": "finresearch.evidence_merge.v1",
        "tool_version": __version__,
        "merged_at": _iso_utc(merged_at),
        "pit_filter_applied": False,
        "minimum_as_of_for_all_evidence": _iso_utc(minimum_as_of),
        "duplicate_evidence_id_policy": "reject",
        "artifact_layout": "artifacts/sha256/<raw_sha256>",
        "limits": vars(effective_limits),
        "inputs": [
            {
                "input_ordinal": item.ordinal,
                "bundle_sha256": item.bundle_sha256,
                "evidence_count": len(item.evidence),
                "artifact_count": len(input_artifacts[item.ordinal]),
            }
            for item in loaded
        ],
        "records": records,
        "artifacts": artifact_manifest,
        "output": {
            "evidence_path": "evidence.json",
            "evidence_sha256": evidence_sha256,
            "evidence_count": len(transformed),
            "artifact_count": len(artifacts),
        },
    }
    manifest_bytes = _manifest_bytes(manifest)

    published_output_dir: Path | None = None
    parent_fd: int | None = None
    staging_name: str | None = None
    staging_root_fd: int | None = None
    artifacts_fd: int | None = None
    sha256_fd: int | None = None
    durability_confirmed = True
    try:
        parent_path, parent_fd = _open_output_parent(output_dir)
        output_name = output_dir.name
        if not output_name or output_name in {".", ".."}:
            raise EvidenceMergeError("output_error", "output path is invalid")
        published_output_dir = parent_path / output_name
        if _entry_exists_at(parent_fd, output_name):
            raise EvidenceMergeError("output_error", "output path already exists")
        if not _directory_path_matches_fd(parent_path, parent_fd):
            raise EvidenceMergeError("output_error", "output parent changed")
        staging_name, staging_root_fd = _create_private_staging_at(parent_fd)
        if not _directory_entry_matches_fd(parent_fd, staging_name, staging_root_fd):
            raise EvidenceMergeError("output_error", "private staging directory changed")
        if artifacts:
            artifacts_fd = _create_directory_at(staging_root_fd, "artifacts")
            sha256_fd = _create_directory_at(artifacts_fd, "sha256")
        for digest, plan in sorted(artifacts.items()):
            if sha256_fd is None:
                raise EvidenceMergeError("output_error", "staged artifact directory is missing")
            _copy_artifact(
                plan,
                sha256_fd,
                digest,
                effective_limits.max_single_artifact_bytes,
            )
        _write_file_at_fsynced(staging_root_fd, "evidence.json", evidence_bytes)
        _write_file_at_fsynced(staging_root_fd, "merge.json", manifest_bytes)
        _validate_staged_output(
            staging_root_fd,
            artifacts_fd,
            sha256_fd,
            evidence_bytes,
            manifest_bytes,
            transformed,
            artifacts,
        )
        if sha256_fd is not None:
            os.fsync(sha256_fd)
        if artifacts_fd is not None:
            os.fsync(artifacts_fd)
        os.fsync(staging_root_fd)
        # Fail before the commit point when the parent cannot be synchronized
        # at all. A failure after rename is reported as an explicit durability
        # warning rather than the ambiguous state "error plus valid output".
        os.fsync(parent_fd)
        if _entry_exists_at(parent_fd, output_name):
            raise EvidenceMergeError("output_error", "output path already exists")
        if not _directory_path_matches_fd(parent_path, parent_fd):
            raise EvidenceMergeError("output_error", "output parent changed")
        if not _directory_entry_matches_fd(parent_fd, staging_name, staging_root_fd):
            raise EvidenceMergeError("output_error", "private staging directory changed")
        _rename_directory_noreplace_at(
            parent_fd,
            staging_name,
            output_name,
            published_output_dir,
        )
        try:
            os.fsync(parent_fd)
        except (OSError, RuntimeError):
            durability_confirmed = False
        try:
            if not _directory_entry_matches_fd(parent_fd, output_name, staging_root_fd):
                raise EvidenceMergeError("output_error", "published directory identity mismatch")
            _validate_staged_output(
                staging_root_fd,
                artifacts_fd,
                sha256_fd,
                evidence_bytes,
                manifest_bytes,
                transformed,
                artifacts,
            )
            if not _directory_path_matches_fd(parent_path, parent_fd):
                raise EvidenceMergeError("output_error", "output parent changed")
        except (EvidenceMergeError, OSError, RuntimeError, ValueError) as exc:
            if not _directory_entry_matches_fd(parent_fd, output_name, staging_root_fd):
                raise EvidenceMergeError(
                    "output_error", "published output failed final validation"
                ) from exc
            try:
                _rename_directory_noreplace_at(
                    parent_fd,
                    output_name,
                    staging_name,
                    parent_path / staging_name,
                )
            except OSError as rollback_exc:
                raise EvidenceMergeError(
                    "output_error",
                    "published output failed final validation and rollback",
                ) from rollback_exc
            with suppress(OSError, RuntimeError):
                os.fsync(parent_fd)
            raise EvidenceMergeError(
                "output_error", "published output failed final validation"
            ) from exc
        staging_name = None
    except EvidenceMergeError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceMergeError("output_error", "failed to publish merged evidence") from exc
    finally:
        for descriptor in (sha256_fd, artifacts_fd, staging_root_fd, parent_fd):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

        # Deliberately do not delete failed staging trees here. POSIX has no
        # portable conditional unlink/rmdir by inode, so automatic cleanup can
        # delete entries concurrently substituted by another same-UID process.
        # The random mode-0700 tree is retained for a non-concurrent janitor.
        del staging_name

    if published_output_dir is None:
        raise EvidenceMergeError("output_error", "failed to publish merged evidence")
    return MergeResult(
        evidence_path=published_output_dir / "evidence.json",
        manifest_path=published_output_dir / "merge.json",
        evidence_ids=tuple(item.evidence_id for item in transformed),
        minimum_as_of_for_all_evidence=minimum_as_of,
        input_count=len(loaded),
        evidence_count=len(transformed),
        artifact_count=len(artifacts),
        durability_confirmed=durability_confirmed,
    )


def merge_evidence_bundles(
    input_paths: Sequence[Path],
    output_dir: Path,
    *,
    limits: MergeLimits | None = None,
    clock: Callable[[], datetime] = _now_utc,
) -> MergeResult:
    """Merge EvidenceBundles into one self-contained, auditable directory.

    The operation validates structural and cryptographic integrity. It does not
    authenticate the upstream publisher or apply an allowlist/PIT filter, and it
    preserves every evidence timestamp for the later research gate.
    """

    effective_limits = limits or DEFAULT_MERGE_LIMITS
    if not isinstance(effective_limits, MergeLimits):
        raise EvidenceMergeError("input_error", "limits must be a MergeLimits instance")
    _require_safe_filesystem_capabilities()
    output_dir = Path(output_dir)
    if os.path.lexists(output_dir):
        raise EvidenceMergeError("output_error", "output path already exists")

    with ExitStack() as descriptors:
        loaded, _ = _load_inputs(
            tuple(input_paths),
            effective_limits,
            descriptors,
        )
        return _merge_loaded_inputs(
            loaded,
            output_dir,
            effective_limits=effective_limits,
            clock=clock,
        )
