"""Tests for the ``compare.run`` JSON-RPC handler (tui_gateway/methods_compare.py).

The handler module is installed onto a stand-in server namespace (the same ``bind_module``
seam server.py uses), so the tests exercise argument validation and the result shape without
importing the whole server. The comparison itself is stubbed.
"""

from __future__ import annotations

import logging
import types
from unittest.mock import patch

import pytest

from agent.model_compare import CompareResult


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, msg, data=None):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def _int_param(params, key, default):
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


@pytest.fixture()
def server():
    fake = types.ModuleType("fake_server")
    fake._ok, fake._err, fake._int_param = _ok, _err, _int_param
    fake._sessions = {}
    fake._methods = {}
    fake.logger = logging.getLogger("fake_server")
    fake._profile_scoped = lambda handler: handler
    from tui_gateway import methods_compare
    methods_compare.register(fake)
    return fake


def _call(server, **params):
    return server._methods["compare.run"](7, params)


def test_registered_and_long_running():
    from tui_gateway import server as real
    assert "compare.run" in real._LONG_HANDLERS
    assert "compare.options" in real._LONG_HANDLERS
    assert "compare.options" in real._methods
    assert "compare.run" in real._methods


def test_prompt_required(server):
    resp = _call(server, candidates=[{"provider": "p", "model": "m"}])
    assert resp["error"]["code"] == 4040


def test_candidates_required(server):
    resp = _call(server, prompt="hi", candidates=[{"model": ""}])
    assert resp["error"]["code"] == 4041
    assert "candidates required" in resp["error"]["message"]


def test_runs_and_returns_result_dicts(server):
    captured = {}

    def fake_run(candidates, prompt, **kw):
        captured["candidates"] = candidates
        captured["kw"] = kw
        return [CompareResult(c.provider, c.model, c.display, text=f"{c.model}!", elapsed_s=0.5) for c in candidates]

    with patch("agent.model_compare.run_comparison", fake_run):
        resp = _call(
            server, prompt="q", candidates=[{"provider": "a", "model": "m1"}, "b:m2"],
            max_tokens=999999, temperature="0.7", timeout=1, system="be terse")
    results = resp["result"]["results"]
    assert [(r["label"], r["text"], r["ok"]) for r in results] == [("a:m1", "m1!", True), ("b:m2", "m2!", True)]
    assert resp["result"]["prompt"] == "q"
    assert captured["kw"]["max_tokens"] == 16384  # capped
    assert captured["kw"]["temperature"] == 0.7
    assert captured["kw"]["timeout"] == 5.0  # floor
    assert captured["kw"]["system_prompt"] == "be terse"
    assert captured["kw"]["history"] is None


def test_single_string_candidate_and_history_from_live_session(server):
    server._sessions["sid"] = {"history": [{"role": "user", "content": "earlier"}], "agent": None}
    captured = {}

    def fake_run(candidates, prompt, **kw):
        captured.update(kw)
        return []

    with patch("agent.model_compare.run_comparison", fake_run):
        resp = _call(server, prompt="q", candidates="p:m", session_id="sid", include_history=True)
    assert resp["result"]["results"] == []
    assert captured["history"] == [{"role": "user", "content": "earlier"}]


def test_failure_is_an_rpc_error(server):
    with patch("agent.model_compare.run_comparison", side_effect=RuntimeError("boom")):
        resp = _call(server, prompt="q", candidates=[{"provider": "p", "model": "m"}])
    assert resp["error"]["code"] == 5040 and "boom" in resp["error"]["message"]


# ---------------------------------------------------------------------------
# TUI / desktop typed `/compare`: answered from the live server, not the slash worker


def _fake_run_comparison(candidates, prompt, **_kw):
    return [CompareResult(c.provider, c.model, c.display, text=f"answer {c.model} to {prompt}", elapsed_s=1.0)
            for c in candidates]


def test_live_slash_compare_answers_without_the_worker():
    from tui_gateway import server
    session = {"agent": types.SimpleNamespace(provider="anthropic", model="cur"), "profile_home": None}
    with patch("agent.model_compare.run_comparison", _fake_run_comparison):
        out = server._live_slash_command_output("sid", session, "compare", "--models a:m1,m2 why?")
    assert out is not None
    assert "[1/2] a:m1" in out and "answer m1 to why?" in out
    assert "[2/2] anthropic:m2" in out  # bare name takes the live agent's provider
    assert "Comparison summary" in out


def test_live_slash_compare_without_session_or_models():
    from tui_gateway import server
    assert "Usage: /compare" in server._live_slash_command_output("sid", None, "compare", "")
    rows = [{"slug": "openrouter", "models": ["m1"]}]
    with patch("hermes_cli.inventory.compare_provider_rows", return_value=rows):
        out = server._live_slash_command_output("sid", None, "compare", "just a prompt")
    assert "--models provider:model" in out and "openrouter:m1" in out


def _options_server(server, provider="anthropic", model="cur"):
    server._session_agent = lambda params: None
    server._model_picker_context = lambda agent: types.SimpleNamespace(
        current_provider=provider, current_model=model, current_base_url="")
    return server


def test_options_list_current_route_then_configured_providers(server):
    _options_server(server)
    rows = [
        {"slug": "moa", "models": ["preset"]},
        {"slug": "openrouter", "models": ["a", "b", "c", "d"], "authenticated": True},
        {"slug": "anthropic", "models": ["cur", "next"]},
    ]
    with patch("hermes_cli.inventory.compare_provider_rows", return_value=rows) as listing:
        resp = server._methods["compare.options"](7, {"session_id": "s1", "refresh": True})
    assert resp["result"]["current"] == "anthropic:cur"
    assert resp["result"]["choices"] == [
        "anthropic:cur", "openrouter:a", "openrouter:b", "openrouter:c", "openrouter:d", "anthropic:next"]
    assert listing.call_args.kwargs == {"refresh": True}  # no per-provider cap: the overlay scrolls


def test_options_list_every_model(server):
    _options_server(server)
    rows = [{"slug": "openrouter", "models": [f"m{i}" for i in range(30)]}]
    with patch("hermes_cli.inventory.compare_provider_rows", return_value=rows):
        resp = server._methods["compare.options"](7, {})
    assert len(resp["result"]["choices"]) == 31


def test_options_only_offer_explicitly_configured_providers(server):
    """A resolvable ``copilot`` binary makes copilot-acp "authenticated" for discovery, but the
    picker must not offer it unless the user configured it — the rows go through the explicit filter."""
    _options_server(server)
    payload = {"providers": [
        {"slug": "openrouter", "models": ["a"], "authenticated": True},
        {"slug": "openai", "models": [], "authenticated": False},
    ]}
    with patch("hermes_cli.inventory.build_models_payload", return_value=payload) as build:
        resp = server._methods["compare.options"](7, {})
    assert resp["result"]["choices"] == ["anthropic:cur", "openrouter:a"]
    assert build.call_args.kwargs["explicit_only"] is True
    assert build.call_args.kwargs["max_models"] is None


def test_options_survive_a_failed_listing(server):
    _options_server(server)
    with patch("hermes_cli.inventory.compare_provider_rows", side_effect=RuntimeError("offline")):
        resp = server._methods["compare.options"](7, {})
    assert resp["result"] == {"choices": ["anthropic:cur"], "current": "anthropic:cur", "selected": []}


def test_options_carry_the_saved_selection(server, tmp_path, monkeypatch):
    from agent.model_compare import save_candidates
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    save_candidates(["openrouter:a", "gone:z"], tmp_path)
    _options_server(server)
    with patch("hermes_cli.inventory.compare_provider_rows", return_value=[{"slug": "openrouter", "models": ["a"]}]):
        resp = server._methods["compare.options"](7, {})
    assert resp["result"]["selected"] == ["openrouter:a", "gone:z"]  # the picker drops rows it cannot show


def test_remember_saves_the_selection(server, tmp_path, monkeypatch):
    from agent.model_compare import load_saved_candidates
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    resp = server._methods["compare.remember"](7, {"candidates": ["a:m1", {"provider": "b", "model": "m2"}, "a:m1"]})
    assert resp["result"] == {"models": ["a:m1", "b:m2"]}
    assert load_saved_candidates(tmp_path) == ["a:m1", "b:m2"]
    # An empty list is not a reset: the previous selection stays.
    assert server._methods["compare.remember"](8, {"candidates": []})["result"] == {"models": []}
    assert load_saved_candidates(tmp_path) == ["a:m1", "b:m2"]


def test_live_slash_compare_reuses_and_saves_the_selection(tmp_path, monkeypatch):
    from agent.model_compare import load_saved_candidates, save_candidates
    from tui_gateway import server
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = {"agent": types.SimpleNamespace(provider="anthropic", model="cur"), "profile_home": None}
    with patch("agent.model_compare.run_comparison", _fake_run_comparison):
        server._live_slash_command_output("sid", session, "compare", "--models a:m1,m2 why?")
        assert load_saved_candidates(tmp_path) == ["a:m1", "anthropic:m2"]
        out = server._live_slash_command_output("sid", session, "compare", "again?")
        assert "[1/2] a:m1" in out and "answer m1 to again?" in out and "[2/2] anthropic:m2" in out
        # --pick refuses the memory and lists what is available instead.
        save_candidates(["a:m1"], tmp_path)
        with patch("hermes_cli.inventory.compare_provider_rows", return_value=[{"slug": "openrouter", "models": ["m1"]}]):
            out = server._live_slash_command_output("sid", session, "compare", "--pick again?")
    assert "--models provider:model" in out and "openrouter:m1" in out
