from types import SimpleNamespace

import pytest

from finresearch.llm.base import LLMError
from finresearch.llm.deepseek import DeepSeekAdapter
from finresearch.schemas import ChallengeMemo
from finresearch.settings import DeepSeekSettings


class FakeCompletions:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        content = (
            '{"counter_thesis":{"text":"证据不足以排除潜在反例。",'
            '"claim_ids":["claim-1"],"assumptions":[]},'
            '"challenged_claim_ids":[],"risks":[{"text":"样本数量有限，需要更多数据。",'
            '"claim_ids":["claim-1"],"assumptions":[]}],'
            '"missing_information":["缺少更多期间数据。"]}'
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class SequenceCompletions(FakeCompletions):
    def __init__(self, contents) -> None:
        super().__init__()
        self.contents = iter(contents)
        self.calls = 0

    def create(self, **kwargs):
        self.kwargs = kwargs
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(self.contents)))]
        )


class RaisingCompletions:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def create(self, **kwargs):
        raise RuntimeError(f"upstream failed with {self.secret}")


def test_adapter_uses_reviewed_model_and_json_mode() -> None:
    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    settings = DeepSeekSettings(api_key="sentinel-secret")
    adapter = DeepSeekAdapter(settings, client=client)

    result = adapter.generate("challenge", {"claims": []}, ChallengeMemo)

    assert result.challenged_claim_ids == []
    assert completions.kwargs["model"] == "deepseek-v4-flash"
    assert completions.kwargs["response_format"] == {"type": "json_object"}
    assert completions.kwargs["extra_body"]["reasoning_effort"] == "low"
    assert "sentinel-secret" not in repr(settings)


def test_adapter_retries_invalid_json_once() -> None:
    valid = FakeCompletions().create().choices[0].message.content
    completions = SequenceCompletions(["{", valid])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    adapter = DeepSeekAdapter(DeepSeekSettings(api_key="sentinel-secret"), client=client)

    result = adapter.generate("challenge", {"claims": []}, ChallengeMemo)

    assert result.challenged_claim_ids == []
    assert completions.calls == 2


def test_network_error_redacts_secret() -> None:
    secret = "sentinel-secret"
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=RaisingCompletions(secret))
    )
    adapter = DeepSeekAdapter(DeepSeekSettings(api_key=secret), client=client)

    with pytest.raises(LLMError) as error:
        adapter.generate("challenge", {"claims": []}, ChallengeMemo)

    assert secret not in str(error.value)
