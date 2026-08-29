from __future__ import annotations

import json
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from finresearch.llm.base import LLMError, ModelT
from finresearch.prompts import build_messages
from finresearch.settings import DeepSeekSettings, redact_secret


class DeepSeekAdapter:
    provider = "deepseek"

    def __init__(self, settings: DeepSeekSettings, client: Any | None = None) -> None:
        self.model = settings.model
        self._settings = settings
        try:
            self._client = client or OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
            )
        except Exception as exc:
            safe = redact_secret(str(exc), [settings.api_key])
            raise LLMError(f"DeepSeek client initialization failed: {safe}") from None

    def generate(self, task: str, payload: dict[str, Any], output_model: type[ModelT]) -> ModelT:
        messages = build_messages(task, payload, output_model)
        last_error = "unknown error"
        for _attempt in range(2):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=self._settings.max_tokens,
                    extra_body={
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": self._settings.reasoning_effort,
                    },
                )
                content = response.choices[0].message.content
                if not content:
                    raise ValueError("model returned empty content")
                return output_model.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError, ValueError, IndexError) as exc:
                last_error = str(exc)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一次输出未通过本地校验。"
                            "请重新输出完整且严格符合 schema 的 JSON。"
                        ),
                    }
                )
            except Exception as exc:  # SDK/network errors are normalized and secrets are redacted.
                safe = redact_secret(str(exc), [self._settings.api_key])
                raise LLMError(f"DeepSeek request failed: {safe}") from None
        safe = redact_secret(last_error, [self._settings.api_key])
        raise LLMError(f"DeepSeek returned invalid structured output after 2 attempts: {safe}")
