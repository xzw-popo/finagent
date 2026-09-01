from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NoTradePolicy:
    allowed_capabilities: frozenset[str] = frozenset(
        {
            "read_local_evidence",
            "read_market_quote",
            "call_json_llm",
            "write_local_evidence",
            "write_local_report",
        }
    )

    def authorize(self, capability: str) -> None:
        if capability not in self.allowed_capabilities:
            raise PermissionError(f"capability is not permitted in research-only V1: {capability}")
