from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SENSITIVE_KEY = re.compile(r"api[_-]?key|authorization|password|secret|token", re.IGNORECASE)
SECRET_VALUE = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


def sanitize_audit_details(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            item_key: sanitize_audit_details(item, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_audit_details(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub("[REDACTED]", value)
    return value


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    stage: str
    occurred_at: str
    input_sha256: str
    details: dict[str, Any]


class AuditTrail:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, stage: str, input_value: Any, details: dict[str, Any] | None = None) -> None:
        self.events.append(
            AuditEvent(
                stage=stage,
                occurred_at=datetime.now(UTC).isoformat(),
                input_sha256=stable_hash(input_value),
                details=sanitize_audit_details(details or {}),
            )
        )

    def write(self, path: Path) -> None:
        content = "\n".join(
            json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) for event in self.events
        )
        path.write_text(f"{content}\n", encoding="utf-8")
