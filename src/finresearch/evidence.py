from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path, PureWindowsPath

from finresearch.schemas import Evidence, EvidenceBundle, RejectedEvidence, ResearchRequest


def load_request(path: Path) -> ResearchRequest:
    return ResearchRequest.model_validate_json(path.read_text(encoding="utf-8"))


def evidence_record_sha256(item: Evidence) -> str:
    # Optional/default V1.1 provenance fields are omitted so existing V1 document
    # evidence keeps its original record hash.
    payload = item.model_dump(
        mode="json",
        exclude={"record_sha256"},
        exclude_defaults=True,
        exclude_none=True,
    )
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
        if (
            item.provenance is not None
            and item.provenance.normalized_sha256 != item.content_sha256
        ):
            raise ValueError(
                f"normalized_sha256 mismatch for evidence {item.evidence_id}"
            )
        if evidence_record_sha256(item) != item.record_sha256:
            raise ValueError(f"record_sha256 mismatch for evidence {item.evidence_id}")
    return validated


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _resolve_raw_artifact(
    base_dir: Path,
    raw_artifact_ref: str,
    evidence_id: str,
    *,
    require_file: bool,
) -> Path:
    reference = Path(raw_artifact_ref)
    windows_reference = PureWindowsPath(raw_artifact_ref)
    if (
        not reference.parts
        or reference.is_absolute()
        or windows_reference.is_absolute()
        or bool(windows_reference.drive)
        or ".." in reference.parts
        or ".." in windows_reference.parts
    ):
        raise ValueError(f"unsafe raw artifact reference for evidence {evidence_id}")

    resolved_base = base_dir.resolve()
    artifact = (resolved_base / reference).resolve()
    if not artifact.is_relative_to(resolved_base):
        raise ValueError(f"unsafe raw artifact reference for evidence {evidence_id}")
    if require_file and not artifact.is_file():
        raise ValueError(f"raw artifact missing for evidence {evidence_id}")
    return artifact


def _validate_raw_artifacts(base_dir: Path, evidence: list[Evidence]) -> None:
    verified: dict[Path, str] = {}
    for item in evidence:
        if item.provenance is None:
            continue
        artifact = _resolve_raw_artifact(
            base_dir,
            item.provenance.raw_artifact_ref,
            item.evidence_id,
            require_file=True,
        )
        digest = verified.get(artifact)
        if digest is None:
            digest = _file_sha256(artifact)
            verified[artifact] = digest
        if digest != item.provenance.raw_sha256:
            raise ValueError(f"raw_sha256 mismatch for evidence {item.evidence_id}")


def validate_evidence_artifacts(
    evidence: list[Evidence], source_dir: Path
) -> list[Evidence]:
    """Validate evidence records and every referenced provenance sidecar."""

    validated = validate_evidence_records(evidence)
    _validate_raw_artifacts(source_dir, validated)
    return validated


def copy_evidence_artifacts(
    evidence: list[Evidence], source_dir: Path, output_dir: Path
) -> list[Path]:
    """Copy provenance sidecars into a self-contained evidence directory.

    Relative references are preserved. Existing files are reused only when their
    SHA-256 matches the evidence record; conflicting files are never overwritten.
    """

    evidence = validate_evidence_records(evidence)
    output_dir.mkdir(parents=True, exist_ok=True)
    verified_sources: dict[Path, str] = {}
    copy_plan: dict[Path, tuple[Path, str, str]] = {}

    for item in evidence:
        if item.provenance is None:
            continue
        reference = item.provenance.raw_artifact_ref
        source = _resolve_raw_artifact(
            source_dir,
            reference,
            item.evidence_id,
            require_file=True,
        )
        expected_sha256 = item.provenance.raw_sha256
        source_sha256 = verified_sources.get(source)
        if source_sha256 is None:
            source_sha256 = _file_sha256(source)
            verified_sources[source] = source_sha256
        if source_sha256 != expected_sha256:
            raise ValueError(f"raw_sha256 mismatch for evidence {item.evidence_id}")

        destination = _resolve_raw_artifact(
            output_dir,
            reference,
            item.evidence_id,
            require_file=False,
        )
        existing_plan = copy_plan.get(destination)
        if existing_plan is not None and existing_plan[1] != expected_sha256:
            raise ValueError(
                f"raw artifact reference conflict for evidence {item.evidence_id}"
            )
        copy_plan[destination] = (source, expected_sha256, item.evidence_id)

    materialized: list[Path] = []
    for destination, (source, expected_sha256, evidence_id) in copy_plan.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = _resolve_raw_artifact(
            output_dir,
            str(destination.relative_to(output_dir.resolve())),
            evidence_id,
            require_file=False,
        )
        if destination.exists():
            if not destination.is_file() or _file_sha256(destination) != expected_sha256:
                raise ValueError(
                    f"raw artifact overwrite conflict for evidence {evidence_id}"
                )
            materialized.append(destination)
            continue

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            if _file_sha256(temporary_path) != expected_sha256:
                raise ValueError(f"raw_sha256 mismatch for evidence {evidence_id}")
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                if (
                    not destination.is_file()
                    or _file_sha256(destination) != expected_sha256
                ):
                    raise ValueError(
                        f"raw artifact overwrite conflict for evidence {evidence_id}"
                    ) from None
            materialized.append(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    return materialized


def load_evidence(path: Path) -> list[Evidence]:
    bundle = EvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
    return validate_evidence_artifacts(bundle.evidence, path.parent)


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
        elif (item.available_at or item.known_at) > request.as_of:
            time_field = "available_at" if item.available_at is not None else "known_at"
            rejected.append(
                RejectedEvidence(
                    evidence_id=item.evidence_id,
                    reason=f"{time_field} is after as_of",
                )
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
