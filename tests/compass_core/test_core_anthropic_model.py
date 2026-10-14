from types import SimpleNamespace

import pytest

from compass_core import anthropic_model, llm


class StubClient:
    """Just enough of the SDK client for AnthropicModel: with_options().beta.messages.create()."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.timeouts = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self

    def _create(self, **request):
        self.calls.append(request)
        return self.response


def _response(text="hello", stop_reason="end_turn", **extra):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        **extra,
    )


def test_call_sends_the_model_effort_and_fallback_but_never_temperature():
    client = StubClient(_response("answer"))
    model = anthropic_model.AnthropicModel({"anthropic": {"effort": "low"}}, client=client)

    out = model.call([{"role": "user", "content": "q"}], max_tokens=400, temperature=0.2, timeout=45, system_prompt="sys")

    assert out == "answer"
    (request,) = client.calls
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"] == {"effort": "low"}
    assert request["system"] == "sys"
    assert request["max_tokens"] == 400
    assert request["messages"] == [{"role": "user", "content": "q"}]
    assert request["betas"] == [anthropic_model.FALLBACK_BETA] and request["fallbacks"] == "default"
    assert "temperature" not in request and "thinking" not in request
    assert client.timeouts == [45.0]
    assert model.name == "claude-opus-5-5"


def test_settings_come_from_config_then_env(monkeypatch):
    monkeypatch.setenv("COMPASS_ANTHROPIC_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("COMPASS_ANTHROPIC_EFFORT", "high")
    env_model = anthropic_model.AnthropicModel({}, client=StubClient(_response()))
    assert (env_model.model, env_model.effort, env_model.fallbacks) == ("claude-sonnet-5-5", "high", True)

    cfg_model = anthropic_model.AnthropicModel({"anthropic": {"model": "m", "fallbacks": False}}, client=StubClient(_response()))
    assert (cfg_model.model, cfg_model.fallbacks) == ("m", False)
    (request,) = (cfg_model.call([{"role": "user", "content": "q"}]), cfg_model._client.calls)[1]
    assert "fallbacks" not in request and "betas" not in request


def test_a_refusal_is_an_error_with_its_category():
    client = StubClient(_response("", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")))
    model = anthropic_model.AnthropicModel({}, client=client)
    with pytest.raises(RuntimeError, match="declined.*cyber"):
        model.call([{"role": "user", "content": "q"}])


def test_available_needs_the_sdk_and_a_credential(monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "no-such-dir"))
    assert anthropic_model.available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert anthropic_model.available() is True
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "default.json").write_text("{}")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "profiles"))
    assert anthropic_model.available() is True


def test_install_is_a_no_op_without_the_sdk(monkeypatch):
    monkeypatch.setattr(anthropic_model, "available", lambda: False)
    previous = llm._factory
    try:
        llm.set_model_factory(None)
        assert anthropic_model.install_if_available() is False
        assert llm.model_for({}) is None
    finally:
        llm.set_model_factory(previous)


def test_install_uses_the_factory_when_the_sdk_is_present(monkeypatch):
    monkeypatch.setattr(anthropic_model, "available", lambda: True)
    monkeypatch.setattr(anthropic_model, "AnthropicModel", lambda config, client=None: SimpleNamespace(name="stub", config=config))
    previous = llm._factory
    try:
        assert anthropic_model.install_if_available() is True
        assert llm.model_for({"x": 1}).config == {"x": 1}
    finally:
        llm.set_model_factory(previous)
