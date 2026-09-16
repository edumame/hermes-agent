"""Tests for Discord clarify button rendering and resolution.

Mirrors test_telegram_clarify_buttons.py for the Discord ``send_clarify``
override and the ``ClarifyChoiceView`` callbacks. Discord uses ``discord.ui.View``
button callbacks (closures) rather than a string-prefixed callback_query
dispatcher like Telegram — the auth + resolution path is the same:

  · numeric choice → resolve_gateway_clarify(clarify_id, choice_text)
  · "Other" button → mark_awaiting_text(clarify_id) so the text-intercept
    captures the next user message in this session
  · already-resolved or unauthorized → ephemeral "this prompt..." reply
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Repo root importable
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# Triggers the shared discord mock from tests/gateway/conftest.py before
# importing the production module.
from plugins.platforms.discord.adapter import (  # noqa: E402
    ClarifyChoiceView,
    DiscordAdapter,
)
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import utf16_len  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(*, allowed_users=None, allowed_roles=None):
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set(allowed_users or [])
    adapter._allowed_role_ids = set(allowed_roles or [])
    return adapter


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_interaction(*, user_id="42", display_name="Tester", roles=None,
                      include_message=True):
    """Build a mock discord.Interaction with response.edit_message /
    send_message / defer all coroutine-callable."""
    user = SimpleNamespace(
        id=user_id,
        display_name=display_name,
        roles=[SimpleNamespace(id=r) for r in (roles or [])],
    )
    response = SimpleNamespace(
        edit_message=AsyncMock(),
        send_message=AsyncMock(),
        defer=AsyncMock(),
    )
    if include_message:
        embed = MagicMock()
        embed.color = None
        embed.set_footer = MagicMock()
        message = SimpleNamespace(embeds=[embed])
    else:
        message = None
    return SimpleNamespace(user=user, response=response, message=message)


# ===========================================================================
# ClarifyChoiceView construction
# ===========================================================================

class TestClarifyChoiceViewConstruction:
    """The view should build numeric buttons plus an Other button."""


    def test_truncates_long_choice_label(self):
        long_choice = "x" * 200
        view = ClarifyChoiceView(
            choices=[long_choice],
            clarify_id="cidZ",
            allowed_user_ids=set(),
        )
        # 78 chars + single-char ellipsis in the body, plus "1. " prefix.
        # Uses U+2026 (…) instead of "..." to fit the 80-char Discord cap.
        first_label = view.children[0].label
        assert first_label.startswith("1. ")
        assert first_label.endswith("\u2026")
        # Final label total <= 80 (Discord cap on button labels)
        assert len(first_label) <= 80


    def test_truncates_long_no_space_choice_on_soft_boundary(self):
        # A long choice with soft boundaries (commas, hyphens) but no spaces
        # should still cut on a soft boundary, not mid-word. We use an input
        # where position 76 is NOT a soft boundary — the test only passes
        # if the renderer actively searches backward for a soft char
        # rather than blindly cutting at the budget limit.
        long_choice = "a" * 30 + "-" + "b" * 30 + "-" + "c" * 30 + "-" + "d" * 30
        # 30a-30b-30c-30d = 30 + 1 + 30 + 1 + 30 + 1 + 30 = 123 chars
        # Position 76 is 'b' (a mid-word alpha). The renderer must look back
        # for a '-' to cut on.
        view = ClarifyChoiceView(
            choices=[long_choice],
            clarify_id="cidSB",
            allowed_user_ids=set(),
        )
        first_label = view.children[0].label
        assert first_label.endswith("\u2026")
        assert len(first_label) <= 80
        body = first_label[len("1. "):].rstrip("\u2026")
        last_char = body[-1]
        assert last_char in {"-", ",", ".", ")", " "}, (
            f"Label cuts mid-word at {last_char!r}: {first_label!r}"
        )


# ===========================================================================
# Choice callback → resolve_gateway_clarify
# ===========================================================================

class TestClarifyChoiceResolve:
    """Clicking a numeric button should resolve the clarify entry."""

    def setup_method(self):
        _clear_clarify_state()


    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        from tools import clarify_gateway as cm
        cm.register("cidC", "sk-C", "Pick", ["x"])

        # Allowlist set, user not in it
        view = ClarifyChoiceView(
            choices=["x"],
            clarify_id="cidC",
            allowed_user_ids={"99999"},  # not 42
        )

        interaction = _make_interaction(user_id="42")
        await view._resolve_choice(interaction, index=0, choice="x")

        # Ephemeral rejection, no resolution, no edit
        interaction.response.send_message.assert_called_once()
        kwargs = interaction.response.send_message.call_args.kwargs
        assert kwargs.get("ephemeral") is True
        interaction.response.edit_message.assert_not_called()
        with cm._lock:
            entry = cm._entries.get("cidC")
        assert entry is not None
        assert not entry.event.is_set()


# ===========================================================================
# "Other" button → mark_awaiting_text
# ===========================================================================

class TestClarifyOtherButton:
    """Clicking Other should flip the entry into text-capture mode."""

    def setup_method(self):
        _clear_clarify_state()


    @pytest.mark.asyncio
    async def test_other_unauthorized_user_rejected(self):
        from tools import clarify_gateway as cm
        cm.register("cidE", "sk-E", "Pick", ["x"])

        view = ClarifyChoiceView(
            choices=["x"],
            clarify_id="cidE",
            allowed_user_ids={"99999"},
        )

        interaction = _make_interaction(user_id="42")
        await view._on_other(interaction)

        # Rejected; entry NOT awaiting text
        interaction.response.send_message.assert_called_once()
        pending = cm.get_pending_for_session("sk-E")
        assert pending is None or pending.awaiting_text is False


# ===========================================================================
# DiscordAdapter.send_clarify integration
# ===========================================================================

class TestDiscordSendClarify:
    """Verify send_clarify renders an embed and (optionally) attaches the view."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_choice_attaches_view(self):
        adapter = _make_adapter(allowed_users={"42"})
        channel = MagicMock()
        sent_msg = MagicMock()
        sent_msg.id = 123456
        channel.send = AsyncMock(return_value=sent_msg)
        adapter._client.get_channel = MagicMock(return_value=channel)

        result = await adapter.send_clarify(
            chat_id="9001",
            question="Pick a color",
            choices=["red", "green", "blue"],
            clarify_id="cidM",
            session_key="sk-M",
        )

        assert result.success is True
        assert result.message_id == "123456"
        # Verify channel.send was called with embed + view kwargs
        channel.send.assert_called_once()
        kwargs = channel.send.call_args.kwargs
        assert "embed" in kwargs
        assert "view" in kwargs
        assert isinstance(kwargs["view"], ClarifyChoiceView)
        # 3 choice buttons + 1 Other
        assert len(kwargs["view"].children) == 4

    @pytest.mark.asyncio
    async def test_open_ended_omits_view(self):
        adapter = _make_adapter()
        channel = MagicMock()
        sent_msg = MagicMock()
        sent_msg.id = 222
        channel.send = AsyncMock(return_value=sent_msg)
        adapter._client.get_channel = MagicMock(return_value=channel)

        result = await adapter.send_clarify(
            chat_id="9001",
            question="What is your name?",
            choices=None,
            clarify_id="cidOE",
            session_key="sk-OE",
        )

        assert result.success is True
        channel.send.assert_called_once()
        kwargs = channel.send.call_args.kwargs
        # Open-ended path renders embed but no view (text-capture handles reply)
        assert "embed" in kwargs
        assert "view" not in kwargs


    @pytest.mark.asyncio
    async def test_unwrap_does_not_pick_value_or_name_alone(self):
        # 'name' and 'value' are Discord-component-shaped fields that could
        # accidentally appear in dicts not intended as choices (e.g., a
        # developer-error in the gateway wiring). The renderer should not
        # surface them as button labels — only the well-known LLM tool-call
        # keys (label, description, text, title) should win.
        adapter = _make_adapter()
        channel = MagicMock()
        sent_msg = MagicMock()
        sent_msg.id = 888
        channel.send = AsyncMock(return_value=sent_msg)
        adapter._client.get_channel = MagicMock(return_value=channel)

        await adapter.send_clarify(
            chat_id="9001",
            question="?",
            choices=[
                {"name": "only_name_here"},   # should be filtered out
                {"value": "only_value_here"},  # should be filtered out
                {"description": "real choice"},
            ],
            clarify_id="cidNV",
            session_key="sk-NV",
        )
        kwargs = channel.send.call_args.kwargs
        view = kwargs["view"]
        choice_labels = [b.label for b in view.children[:-1]]  # exclude Other
        # Only the well-formed dict survives.
        assert len(choice_labels) == 1, (
            f"Expected 1 choice, got {len(choice_labels)}: {choice_labels!r}"
        )
        assert "real choice" in choice_labels[0]
        for label in choice_labels:
            assert "only_name_here" not in label, f"name leaked: {label!r}"
            assert "only_value_here" not in label, f"value leaked: {label!r}"


# ===========================================================================
# Multi-select view: toggle buttons + ✅ Done
# ===========================================================================

class TestClarifyChoiceViewMultiSelect:
    def setup_method(self):
        _clear_clarify_state()

    def test_multi_view_renders_checkbox_labels_and_done(self):
        view = ClarifyChoiceView(choices=["red", "green"], clarify_id="m1", allowed_user_ids=set(), multi_select=True)
        labels = [child.label for child in view.children]
        assert labels == ["☐ 1. red", "☐ 2. green", "✅ Done", "✏️ Other (type answer)"]
        assert view.children[2].custom_id == "clarify:m1:done"

    def test_multi_view_reserves_a_slot_for_done(self):
        view = ClarifyChoiceView(choices=[f"c{i}" for i in range(30)], clarify_id="m2", allowed_user_ids=set(), multi_select=True)
        assert len(view.choices) == 23 and len(view.children) == 25

    @pytest.mark.asyncio
    async def test_toggle_updates_entry_and_button_without_resolving(self):
        from tools import clarify_gateway as cm
        cm.register("m3", "sk", "Pick", ["red", "green"], multi_select=True)
        view = ClarifyChoiceView(choices=["red", "green"], clarify_id="m3", allowed_user_ids={"42"}, multi_select=True)
        interaction = _make_interaction()
        await view.children[1].callback(interaction)
        assert cm.selected_labels("m3") == ["green"]
        assert view.children[1].label == "☑ 2. green"
        assert view.resolved is False
        interaction.response.edit_message.assert_awaited_once()
        with cm._lock:
            assert not cm._entries["m3"].event.is_set()
        # Toggle back off.
        await view.children[1].callback(interaction)
        assert cm.selected_labels("m3") == [] and view.children[1].label == "☐ 2. green"

    @pytest.mark.asyncio
    async def test_done_resolves_with_json_array(self):
        from tools import clarify_gateway as cm
        cm.register("m4", "sk", "Pick", ["red", "green", "blue"], multi_select=True)
        view = ClarifyChoiceView(choices=["red", "green", "blue"], clarify_id="m4", allowed_user_ids={"42"}, multi_select=True)
        interaction = _make_interaction()
        await view.children[0].callback(interaction)
        await view.children[2].callback(interaction)
        await view.children[3].callback(interaction)  # Done
        with cm._lock:
            entry = cm._entries["m4"]
        assert entry.event.is_set() and entry.response == '["red", "blue"]'
        assert view.resolved is True
        assert all(child.disabled for child in view.children)

    @pytest.mark.asyncio
    async def test_done_with_nothing_selected_is_ephemeral_nudge(self):
        from tools import clarify_gateway as cm
        cm.register("m5", "sk", "Pick", ["red"], multi_select=True)
        view = ClarifyChoiceView(choices=["red"], clarify_id="m5", allowed_user_ids={"42"}, multi_select=True)
        interaction = _make_interaction()
        await view.children[1].callback(interaction)  # Done
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args[1]["ephemeral"] is True
        assert view.resolved is False
        with cm._lock:
            assert not cm._entries["m5"].event.is_set()

    @pytest.mark.asyncio
    async def test_unauthorized_toggle_rejected(self):
        from tools import clarify_gateway as cm
        cm.register("m6", "sk", "Pick", ["red"], multi_select=True)
        view = ClarifyChoiceView(choices=["red"], clarify_id="m6", allowed_user_ids={"42"}, multi_select=True)
        interaction = _make_interaction(user_id="999")
        await view.children[0].callback(interaction)
        assert cm.selected_labels("m6") == []
        interaction.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_clarify_passes_multi_select_from_the_entry(self):
        from tools import clarify_gateway as cm
        cm.register("m7", "sk", "Pick", ["red", "green"], multi_select=True)
        adapter = _make_adapter()
        captured = {}

        async def fake_send_prompt(chat_id, metadata, build, fail_log=None):
            send_kwargs, view = build(MagicMock())
            captured["view"] = view
            captured["kwargs"] = send_kwargs
            return MagicMock(success=True)

        adapter._send_prompt = fake_send_prompt
        await adapter.send_clarify(chat_id="1", question="Pick", choices=["red", "green"], clarify_id="m7", session_key="sk")
        assert captured["view"].multi_select is True
        assert "✅ Done" in captured["kwargs"]["content"]
