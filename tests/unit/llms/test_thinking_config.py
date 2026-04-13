"""Tests for the ThinkingConfig discriminated union.

Claude Code 2.1.x sends ``thinking.type = "adaptive"``; older/reference callers
send ``"enabled"`` or ``"disabled"``. All three must parse so MessageRequest
does not reject real traffic with HTTP 422.
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from ccproxy.llms.models import anthropic as anthropic_models


_thinking_adapter = TypeAdapter(anthropic_models.ThinkingConfig)


@pytest.mark.unit
def test_thinking_config_enabled_round_trip() -> None:
    cfg = _thinking_adapter.validate_python(
        {"type": "enabled", "budget_tokens": 4096}
    )
    assert isinstance(cfg, anthropic_models.ThinkingConfigEnabled)
    assert cfg.type == "enabled"
    assert cfg.budget_tokens == 4096


@pytest.mark.unit
def test_thinking_config_disabled_round_trip() -> None:
    cfg = _thinking_adapter.validate_python({"type": "disabled"})
    assert isinstance(cfg, anthropic_models.ThinkingConfigDisabled)
    assert cfg.type == "disabled"


@pytest.mark.unit
def test_thinking_config_adaptive_round_trip() -> None:
    cfg = _thinking_adapter.validate_python({"type": "adaptive"})
    assert isinstance(cfg, anthropic_models.ThinkingConfigAdaptive)
    assert cfg.type == "adaptive"
    assert cfg.budget_tokens is None


@pytest.mark.unit
def test_thinking_config_adaptive_with_budget() -> None:
    cfg = _thinking_adapter.validate_python(
        {"type": "adaptive", "budget_tokens": 8192}
    )
    assert isinstance(cfg, anthropic_models.ThinkingConfigAdaptive)
    assert cfg.budget_tokens == 8192


@pytest.mark.unit
def test_thinking_config_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        _thinking_adapter.validate_python({"type": "experimental"})


@pytest.mark.unit
def test_create_message_request_accepts_adaptive_thinking() -> None:
    """Regression: CreateMessageRequest used to 422 on Claude Code 2.1.98+ traffic."""
    req = anthropic_models.CreateMessageRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "adaptive"},
        }
    )
    assert req.thinking is not None
    assert req.thinking.type == "adaptive"
