"""Tests for the /compare gateway slash command (gateway/slash_commands_compare.py).

The handler is a fire-and-forget flow like /btw: it returns "" (nothing to double-post) and a
background task drives the multi-select poll, the fan-out and the per-answer posts. These tests
drive that task with a fake adapter and a stubbed comparison so no model is called.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands_compare import (
    COMPARE_POLL_MAX_CHOICES, _decode_poll_reply, _parse_compare_args, _poll_choices,
)


class TestArgParsing:
    def test_models_flag_is_extracted_anywhere(self):
        assert _parse_compare_args("--models a:b,c:d what is up") == (["a:b", "c:d"], "what is up")
        assert _parse_compare_args("what --models=a:b is up") == (["a:b"], "what is up")
        assert _parse_compare_args("--model a:b hi") == (["a:b"], "hi")

    def test_no_flag(self):
        assert _parse_compare_args("  plain prompt ") == ([], "plain prompt")
        assert _parse_compare_args("") == ([], "")


class TestPollReply:
    def test_json_array_from_multi_select(self):
        assert _decode_poll_reply(json.dumps(["a:b", "c:d"])) == ["a:b", "c:d"]

    def test_sentinels_and_empty_mean_nothing(self):
        assert _decode_poll_reply("[user did not respond within 10m]") == []
        assert _decode_poll_reply("[clarify prompt could not be delivered]") == []
        assert _decode_poll_reply("") == []

    def test_typed_single_or_comma_labels(self):
        assert _decode_poll_reply("a:b") == ["a:b"]
        assert _decode_poll_reply("a:b, c:d") == ["a:b", "c:d"]


class TestPollChoices:
    def test_current_route_first_then_providers_capped(self):
        providers = [
            {"slug": "openrouter", "models": ["m1", "m2", "m3", "m4"]},
            {"slug": "moa", "models": ["preset"]},
            {"slug": "anthropic", "models": ["c1", "c2"]},
            {"slug": "google", "models": ["g1", "g2", "g3"]},
        ]
        rows = _poll_choices(providers, current_provider="anthropic", current_model="c1")
        assert rows[0] == "anthropic:c1"
        assert "moa:preset" not in rows
        assert rows.count("anthropic:c1") == 1  # deduped against the provider listing
        assert len(rows) == COMPARE_POLL_MAX_CHOICES

    def test_no_current_model(self):
        assert _poll_choices([{"slug": "x", "models": ["a"]}], current_provider="", current_model="") == ["x:a"]


# ---------------------------------------------------------------------------
# Handler flow


def _make_event(text: str, platform=Platform.TELEGRAM):
    source = SessionSource(platform=platform, user_id="1", chat_id="c1", user_name="tester")
    return MessageEvent(text=text, source=source)


class _FakeAdapter:
    typed_command_prefix = "/"

    def __init__(self):
        self.sent: list[str] = []
        self.clarify_calls: list[dict] = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="1")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.clarify_calls.append({"clarify_id": clarify_id, "choices": list(choices), "session_key": session_key})
        return SendResult(success=True, message_id="2")


def _make_runner(adapter):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._background_tasks = set()
    runner._session_model_overrides = {}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda source: f"telegram:{source.chat_id}"
    runner._adapter_for_source = lambda source: adapter
    runner._reply_metadata = lambda event: None
    return runner


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _fake_run_comparison(candidates, prompt, *, on_result=None, **_kw):
    from agent.model_compare import CompareResult
    results = []
    for i, cand in enumerate(candidates):
        r = CompareResult(cand.provider, cand.model, cand.display, text=f"answer {cand.model} to {prompt}", elapsed_s=1.0)
        results.append(r)
        if on_result is not None:
            on_result(r, i + 1, len(candidates))
    return results


async def _drain(runner):
    while runner._background_tasks:
        await asyncio.gather(*list(runner._background_tasks))


class TestHandleCompareCommand:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_no_prompt_shows_usage(self):
        runner = _make_runner(_FakeAdapter())
        result = await runner._handle_compare_command(_make_event("/compare"))
        assert "Usage:" in result and "/compare" in result and "--models" in result

    @pytest.mark.asyncio
    async def test_pinned_models_skip_the_poll_and_print_every_answer(self):
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        listing = AsyncMock(return_value=([], "anthropic", "cur"))
        with patch.object(runner, "_compare_listing", listing), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            reply = await runner._handle_compare_command(_make_event("/compare --models a:m1,b:m2 why?"))
            assert reply == ""  # the poll/progress messages are the visible reply
            await _drain(runner)
        assert adapter.clarify_calls == []
        assert adapter.sent[0].startswith("🔬 Comparing 2 models: a:m1, b:m2")
        assert "[1/2] a:m1" in adapter.sent[1] and "answer m1 to why?" in adapter.sent[1]
        assert "[2/2] b:m2" in adapter.sent[2] and "answer m2 to why?" in adapter.sent[2]
        assert adapter.sent[3].startswith("📊 **Comparison summary**")

    @pytest.mark.asyncio
    async def test_poll_is_multi_select_and_selection_drives_the_run(self):
        from tools import clarify_gateway as cm
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        providers = [{"slug": "openrouter", "models": ["m1", "m2"]}, {"slug": "anthropic", "models": ["c1"]}]
        listing = AsyncMock(return_value=(providers, "anthropic", "c1"))
        with patch.object(runner, "_compare_listing", listing), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            await runner._handle_compare_command(_make_event("/compare explain monads"))
            # Let the task register + send the poll.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if adapter.clarify_calls:
                    break
            assert adapter.clarify_calls, "poll was never sent"
            call = adapter.clarify_calls[0]
            assert call["choices"][0] == "anthropic:c1"
            assert cm.is_multi_select(call["clarify_id"]) is True
            # The user toggles two rows (button adapters) and presses Done.
            assert cm.toggle_selection(call["clarify_id"], 1) == [1]
            assert cm.toggle_selection(call["clarify_id"], 2) == [1, 2]
            assert cm.resolve_multi_selection(call["clarify_id"]) == json.dumps(call["choices"][1:3])
            await _drain(runner)
        assert adapter.sent[0].startswith("🔬 Comparing 2 models: openrouter:m1, openrouter:m2")
        assert sum("answer m" in m for m in adapter.sent) == 2
        assert adapter.sent[-1].startswith("📊")

    @pytest.mark.asyncio
    async def test_typed_multi_select_reply_resolves_through_text_intercept(self):
        """Platforms without buttons: the numbered-list fallback parses "1, 3" via the clarify primitive."""
        from tools import clarify_gateway as cm
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        providers = [{"slug": "openrouter", "models": ["m1", "m2", "m3"]}]
        with patch.object(runner, "_compare_listing", AsyncMock(return_value=(providers, "", ""))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            await runner._handle_compare_command(_make_event("/compare hi"))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if adapter.clarify_calls:
                    break
            assert cm.attempt_text_response_for_session("telegram:c1", "1, 3") == cm.TEXT_RESOLVED
            await _drain(runner)
        assert adapter.sent[0].startswith("🔬 Comparing 2 models: openrouter:m1, openrouter:m3")

    @pytest.mark.asyncio
    async def test_unanswered_poll_cancels(self):
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        providers = [{"slug": "openrouter", "models": ["m1", "m2"]}]
        with patch.object(runner, "_compare_listing", AsyncMock(return_value=(providers, "", ""))), \
                patch("gateway.slash_commands_compare.COMPARE_POLL_TIMEOUT_S", 1), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison) as run:
            await runner._handle_compare_command(_make_event("/compare hi"))
            await _drain(runner)
        assert adapter.sent == ["🟡 Comparison cancelled — no models were selected."]

    @pytest.mark.asyncio
    async def test_too_few_models_explains(self):
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        with patch.object(runner, "_compare_listing", AsyncMock(return_value=([{"slug": "x", "models": ["only"]}], "", ""))):
            await runner._handle_compare_command(_make_event("/compare hi"))
            await _drain(runner)
        assert adapter.clarify_calls == []
        assert adapter.sent[0].startswith("❌ Fewer than two models")

    @pytest.mark.asyncio
    async def test_poll_send_failure_clears_the_pending_entry(self):
        from tools import clarify_gateway as cm
        adapter = _FakeAdapter()

        async def failing_send_clarify(**_kw):
            return SendResult(success=False, error="nope")

        adapter.send_clarify = failing_send_clarify
        runner = _make_runner(adapter)
        providers = [{"slug": "openrouter", "models": ["m1", "m2"]}]
        with patch.object(runner, "_compare_listing", AsyncMock(return_value=(providers, "", ""))):
            await runner._handle_compare_command(_make_event("/compare hi"))
            await _drain(runner)
        assert cm.has_pending("telegram:c1") is False
        assert adapter.sent == ["🟡 Comparison cancelled — no models were selected."]


class TestSavedSelection:
    """The models of the last comparison are remembered per profile: a bare /compare reuses them
    (no poll), --pick polls again, and every run overwrites the memory."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_bare_compare_reuses_the_last_models_without_a_poll(self, tmp_path):
        from agent.model_compare import save_candidates
        save_candidates(["a:m1", "b:m2"], tmp_path)
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            await runner._handle_compare_command(_make_event("/compare why?"))
            await _drain(runner)
        assert adapter.clarify_calls == []
        assert adapter.sent[0].startswith("🔬 Comparing 2 models (last selection): a:m1, b:m2")
        assert "--pick" in adapter.sent[0]
        assert sum("answer m" in m for m in adapter.sent) == 2

    @pytest.mark.asyncio
    async def test_pick_flag_polls_again_and_the_new_pick_is_saved(self, tmp_path):
        from agent.model_compare import load_saved_candidates, save_candidates
        from tools import clarify_gateway as cm
        save_candidates(["a:m1", "b:m2"], tmp_path)
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        providers = [{"slug": "openrouter", "models": ["m1", "m2"]}]
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=(providers, "", ""))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            await runner._handle_compare_command(_make_event("/compare --pick why?"))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if adapter.clarify_calls:
                    break
            assert adapter.clarify_calls, "poll was never sent"
            assert cm.attempt_text_response_for_session("telegram:c1", "2") == cm.TEXT_RESOLVED
            await _drain(runner)
        assert adapter.sent[0].startswith("🔬 Comparing 1 models: openrouter:m2")
        assert load_saved_candidates(tmp_path) == ["openrouter:m2"]

    @pytest.mark.asyncio
    async def test_pinned_models_are_saved_for_the_next_compare(self, tmp_path):
        from agent.model_compare import load_saved_candidates
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            await runner._handle_compare_command(_make_event("/compare --models a:m1,m2 why?"))
            await _drain(runner)
        assert load_saved_candidates(tmp_path) == ["a:m1", "anthropic:m2"]

    @pytest.mark.asyncio
    async def test_profile_home_scopes_the_memory(self, tmp_path):
        from agent.model_compare import load_saved_candidates
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        runner.config = SimpleNamespace(multiplex_profiles=True)
        profile = tmp_path / "profiles" / "work"
        profile.mkdir(parents=True)
        runner._resolve_profile_home_for_source = lambda source: profile
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison), \
                patch("gateway.run._profile_runtime_scope") as scope:
            scope.return_value.__enter__ = lambda *_a: None
            scope.return_value.__exit__ = lambda *_a: False
            await runner._handle_compare_command(_make_event("/compare --models a:m1 why?"))
            await _drain(runner)
        assert load_saved_candidates(profile) == ["a:m1"]
        assert load_saved_candidates(tmp_path) == []

    @pytest.mark.asyncio
    async def test_cancelled_poll_keeps_the_previous_selection(self, tmp_path):
        from agent.model_compare import load_saved_candidates, save_candidates
        save_candidates(["a:m1"], tmp_path)
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        providers = [{"slug": "openrouter", "models": ["m1", "m2"]}]
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=(providers, "", ""))), \
                patch("gateway.slash_commands_compare.COMPARE_POLL_TIMEOUT_S", 1):
            await runner._handle_compare_command(_make_event("/compare --pick hi"))
            await _drain(runner)
        assert adapter.sent[-1].startswith("🟡 Comparison cancelled")
        assert load_saved_candidates(tmp_path) == ["a:m1"]

    def test_usage_mentions_pick(self):
        runner = _make_runner(_FakeAdapter())
        assert "--pick" in runner._compare_usage(_make_event("/compare"))


@pytest.mark.asyncio
async def test_listing_offers_only_explicitly_configured_providers(tmp_path):
    """The poll's rows go through ``compare_provider_rows`` (explicit configuration only) with the
    session's config context, and the session override wins for the current route."""
    from gateway.slash_commands_model import _ModelSwitchContext

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    runner._session_model_overrides = {"telegram:c1": {"provider": "anthropic", "model": "override"}}
    runner._resolve_session_agent_runtime = lambda source: ("override", {"provider": "anthropic"})
    rows = [{"slug": "openrouter", "models": ["m1"]}]

    def _read_config(self):
        self.current_provider, self.current_model, self.current_base_url = "openrouter", "m1", "https://or"
        self.user_provs = {"openrouter": {"api_key": "k"}}
        self.custom_provs = [{"name": "lab"}]
        self.excluded_provs = ["nous"]

    event = _make_event("/compare hi")
    with patch.object(_ModelSwitchContext, "read_config", _read_config), \
         patch("hermes_cli.inventory.compare_provider_rows", return_value=rows) as listing:
        providers, provider, model = await runner._compare_listing(event, event.source, tmp_path)
    assert (providers, provider, model) == (rows, "anthropic", "override")
    ctx = listing.call_args.args[0]
    assert (ctx.current_provider, ctx.current_model, ctx.current_base_url) == ("anthropic", "override", "https://or")
    assert ctx.user_providers == {"openrouter": {"api_key": "k"}}
    assert ctx.custom_providers == [{"name": "lab"}]
    assert ctx.excluded_providers == ["nous"]
    assert listing.call_args.kwargs == {"max_models": 3}


@pytest.mark.asyncio
async def test_listing_failure_leaves_the_current_route(tmp_path):
    from gateway.slash_commands_model import _ModelSwitchContext

    runner = _make_runner(_FakeAdapter())
    runner._resolve_session_agent_runtime = lambda source: (_ for _ in ()).throw(RuntimeError("no agent"))
    event = _make_event("/compare hi")
    with patch.object(_ModelSwitchContext, "read_config", lambda self: None), \
         patch("hermes_cli.inventory.compare_provider_rows", side_effect=RuntimeError("offline")):
        providers, provider, model = await runner._compare_listing(event, event.source, tmp_path)
    assert (providers, provider, model) == ([], "openrouter", "")


def test_compare_is_a_registered_gateway_command():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
    from hermes_cli.commands_platforms import _SLACK_VIA_HERMES_ONLY
    from gateway.run_busy import GatewayBusySessionMixin

    cmd = resolve_command("compare")
    assert cmd is not None and not cmd.gateway_only and not cmd.cli_only and cmd.busy_policy == "reject"
    assert "compare" in GATEWAY_KNOWN_COMMANDS
    assert "compare" in GatewayBusySessionMixin._IDLE_COMMANDS
    # Slack sits at its 50-slash cap; /compare rides /hermes compare there.
    assert "compare" in _SLACK_VIA_HERMES_ONLY


class TestSavedAsChat:
    """A finished messaging /compare is saved as its own chat in the profile's session store — the
    same writer compare.save uses — so the desktop sidebar and the TUI list it (before, the run lived
    only in the Telegram thread and never reached the desktop)."""

    def setup_method(self):
        _clear_clarify_state()

    @staticmethod
    def _pin_store(runner, tmp_path):
        from hermes_state import AsyncSessionDB, SessionDB
        db = SessionDB(tmp_path / "state.db")
        runner._session_db = AsyncSessionDB(db)  # what the property serves in production
        return db

    @pytest.mark.asyncio
    async def test_finished_run_lands_in_the_session_store(self, tmp_path):
        from gateway.slash_commands_compare import COMPARE_CHAT_SOURCE
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        db = self._pin_store(runner, tmp_path)
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            await runner._handle_compare_command(_make_event("/compare --models a:m1,b:m2 why is the sky blue?"))
            await _drain(runner)

        rows = db.list_sessions_rich(limit=10)
        assert len(rows) == 1
        key = rows[0]["id"]
        row = db.get_session(key)
        assert row["source"] == COMPARE_CHAT_SOURCE  # a LOCAL source: main Chats list, not a Telegram section
        assert db.get_session_title(key) == "Compare: why is the sky blue?"
        messages = db.get_messages(key)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "why is the sky blue?"
        assert "[1/2] a:m1" in messages[1]["content"] and "answer m2 to why is the sky blue?" in messages[1]["content"]
        # The desktop renders the assistant turn as cards from the typed payload.
        assert messages[1]["display_kind"] == "compare"
        # The messaging user learns where the run went.
        assert adapter.sent[-1].startswith("📊 **Comparison summary**")
        assert "💾 Saved as chat “Compare: why is the sky blue?”" in adapter.sent[-1]

    @pytest.mark.asyncio
    async def test_same_prompt_twice_keeps_both_chats(self, tmp_path):
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        db = self._pin_store(runner, tmp_path)
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            for _ in range(2):
                await runner._handle_compare_command(_make_event("/compare --models a:m1 q"))
                await _drain(runner)
        titles = sorted(db.get_session_title(r["id"]) for r in db.list_sessions_rich(limit=10))
        assert titles[0] == "Compare: q" and titles[1].startswith("Compare: q (")

    @pytest.mark.asyncio
    async def test_all_failed_run_is_not_saved(self, tmp_path):
        from agent.model_compare import CompareResult

        def _all_fail(candidates, prompt, *, on_result=None, **_kw):
            results = [CompareResult(c.provider, c.model, c.display, error="boom") for c in candidates]
            for i, r in enumerate(results):
                on_result(r, i + 1, len(results))
            return results

        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        db = self._pin_store(runner, tmp_path)
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _all_fail):
            await runner._handle_compare_command(_make_event("/compare --models a:m1,b:m2 q"))
            await _drain(runner)
        assert db.list_sessions_rich(limit=10) == []
        assert adapter.sent[-1].startswith("📊") and "Saved as chat" not in adapter.sent[-1]

    @pytest.mark.asyncio
    async def test_store_failure_never_loses_the_summary(self, tmp_path):
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        runner._session_db = None  # store unavailable (what the property serves while SQLite is down)
        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison):
            await runner._handle_compare_command(_make_event("/compare --models a:m1 q"))
            await _drain(runner)
        assert adapter.sent[-1].startswith("📊 **Comparison summary**")
        assert "Saved as chat" not in adapter.sent[-1]

    @pytest.mark.asyncio
    async def test_multiplexed_run_is_saved_in_the_routed_profile_store(self, tmp_path):
        """Under multiplexing the save runs inside the profile scope, so the chat lands in THAT
        profile's store (what the desktop lists for the profile), stamped with its name."""
        from hermes_state import AsyncSessionDB, SessionDB
        adapter = _FakeAdapter()
        runner = _make_runner(adapter)
        runner.config = SimpleNamespace(multiplex_profiles=True)
        profile = tmp_path / "profiles" / "work"
        profile.mkdir(parents=True)
        runner._resolve_profile_home_for_source = lambda source: profile
        runner._profile_name_for_source = lambda source, adapter_profile=None: "work"
        profile_db = SessionDB(profile / "state.db")
        scoped_homes: list = []

        # In production the property resolves the store from the scoped HERMES_HOME; pin it here.
        runner._session_db = AsyncSessionDB(profile_db)

        import contextlib

        @contextlib.contextmanager
        def _scope(home, *a, **k):
            scoped_homes.append(home)
            yield

        with patch.dict("os.environ", {"HERMES_HOME": str(tmp_path)}), \
                patch.object(runner, "_compare_listing", AsyncMock(return_value=([], "anthropic", "cur"))), \
                patch("agent.model_compare.run_comparison", _fake_run_comparison), \
                patch("gateway.run._profile_runtime_scope", _scope):
            await runner._handle_compare_command(_make_event("/compare --models a:m1 q"))
            await _drain(runner)
        rows = profile_db.list_sessions_rich(limit=10)
        assert len(rows) == 1
        assert profile_db.get_session(rows[0]["id"])["profile_name"] == "work"
        # Both the fan-out and the save ran inside the profile scope.
        assert scoped_homes == [profile, profile]
