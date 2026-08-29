import pytest

from finresearch.audit import sanitize_audit_details
from finresearch.cli import validate_llm_mode
from finresearch.policy import NoTradePolicy
from finresearch.settings import DeepSeekSettings, redact_secret


def test_trade_capabilities_are_not_authorized() -> None:
    policy = NoTradePolicy()
    with pytest.raises(PermissionError):
        policy.authorize("place_order")


def test_secret_is_not_in_repr_or_normalized_error() -> None:
    secret = "sentinel-secret"
    settings = DeepSeekSettings(api_key=secret)
    assert secret not in repr(settings)
    assert secret not in redact_secret(f"failure involving {secret}", [secret])


def test_unreviewed_deepseek_base_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="reviewed base URL"):
        DeepSeekSettings(api_key="sentinel-secret", base_url="https://attacker.example")


def test_unknown_llm_mode_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported FIN_AGENT_LLM_MODE"):
        validate_llm_mode("typo")


def test_audit_details_redact_sensitive_keys_and_values() -> None:
    secret_value = "sk-" + "a" * 24
    sanitized = sanitize_audit_details(
        {"api_key": "sentinel-secret", "message": f"failure: {secret_value}"}
    )
    assert sanitized == {"api_key": "[REDACTED]", "message": "failure: [REDACTED]"}
