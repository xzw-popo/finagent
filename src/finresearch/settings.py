from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class DeepSeekSettings:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "low"
    max_tokens: int = 4096
    timeout_seconds: float = 90.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY is required in deepseek mode")
        if self.base_url.rstrip("/") != DEFAULT_BASE_URL:
            raise ValueError(f"V1 only permits the reviewed base URL: {DEFAULT_BASE_URL}")
        if self.reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("reasoning_effort must be one of: low, high, max")
        if self.model != DEFAULT_MODEL:
            raise ValueError(f"V1 only permits the reviewed model id: {DEFAULT_MODEL}")

    @classmethod
    def from_environment(cls, api_key: str | None = None) -> DeepSeekSettings:
        return cls(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            reasoning_effort=os.environ.get("DEEPSEEK_REASONING_EFFORT", "low"),
        )


def redact_secret(message: str, secrets: list[str]) -> str:
    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
