from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMAdapter(Protocol):
    provider: str
    model: str

    def generate(self, task: str, payload: dict[str, Any], output_model: type[ModelT]) -> ModelT:
        ...
