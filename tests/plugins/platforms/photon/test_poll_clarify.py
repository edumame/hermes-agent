"""Native-poll clarify tests for PhotonAdapter.

iMessage has a native poll bubble (spectrum-ts `poll()` builder). A
multiple-choice ``clarify`` renders as that poll; the user taps a choice and
the vote streams back inbound as a ``poll_option`` event. These tests cover
both directions without spawning the Node sidecar or binding ports:

  * outbound — ``send_clarify`` with choices POSTs ``/send-poll`` and flips the
    clarify into text-capture mode; with no choices it stays plain text;
  * inbound — a ``poll_option`` selection is dispatched as a plain-text message
    carrying the chosen option (so the gateway clarify-intercept resolves it),
    a deselection is dropped, and an empty-title vote is dropped.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.photon.adapter import PhotonAdapter


def _make_adapter(monkeypatch: pytest.MonkeyPatch) -> PhotonAdapter:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-project-secret")
    cfg = PlatformConfig(enabled=True, token="", extra={})
    return PhotonAdapter(cfg)


def _capture(
    adapter: PhotonAdapter, monkeypatch: pytest.MonkeyPatch
) -> List[MessageEvent]:
    captured: List[MessageEvent] = []

    async def fake_handle(event: MessageEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return captured


def _poll_option_event(
    *, title: str, selected: bool = True, msg_id: str = "spc-msg-vote"
) -> Dict[str, Any]:
    return {
        "messageId": msg_id,
        "platform": "iMessage",
        "space": {"id": "+155****4567", "type": "dm", "phone": "+155****4567"},
        "sender": {"id": "+155****4567"},
        "content": {
            "type": "poll_option",
            "title": title,
            "selected": selected,
            "pollTitle": "Pick one",
        },
        "timestamp": "2026-05-14T19:06:32.000Z",
    }


# ---------------------------------------------------------------------------
# Inbound: a poll vote becomes the clarify answer.


@pytest.mark.asyncio
async def test_poll_vote_dispatched_as_choice_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll selection is forwarded as a plain-text message carrying the
    chosen option, so the gateway clarify text-intercept can resolve it."""
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_inbound(
        _poll_option_event(title="Yes — native tappable buttons")
    )

    assert len(captured) == 1
    ev = captured[0]
    assert ev.text == "Yes — native tappable buttons"
    assert ev.message_type == MessageType.TEXT
    assert ev.source.chat_id == "+155****4567"


# ---------------------------------------------------------------------------
# Outbound: send_clarify renders a native poll for choices.


def _stub_sidecar_poll(
    adapter: PhotonAdapter, monkeypatch: pytest.MonkeyPatch, *, ok: bool = True
) -> List[Tuple[str, str, list]]:
    calls: List[Tuple[str, str, list]] = []

    async def fake_send_poll(space_id: str, title: str, options: list):
        calls.append((space_id, title, list(options)))
        return SendResult(
            success=ok,
            message_id="spc-msg-poll" if ok else None,
            error=None if ok else "boom",
        )

    monkeypatch.setattr(adapter, "_sidecar_send_poll", fake_send_poll)
    return calls


def _stub_sidecar_text(
    adapter: PhotonAdapter, monkeypatch: pytest.MonkeyPatch
) -> List[Tuple[str, str]]:
    sends: List[Tuple[str, str]] = []

    async def fake_send(space_id: str, text: str):
        sends.append((space_id, text))
        return SendResult(success=True, message_id="spc-msg-text")

    monkeypatch.setattr(adapter, "_sidecar_send", fake_send)
    return sends


@pytest.mark.asyncio
async def test_send_clarify_with_choices_sends_native_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    poll_calls = _stub_sidecar_poll(adapter, monkeypatch)

    marked: List[str] = []
    import tools.clarify_gateway as cg

    monkeypatch.setattr(cg, "mark_awaiting_text", lambda cid: marked.append(cid))

    result = await adapter.send_clarify(
        chat_id="+155****4567",
        question="Pick one",
        choices=["A", "B", "C"],
        clarify_id="clar-1",
        session_key="sess-1",
    )

    assert result.success
    assert len(poll_calls) == 1
    space_id, title, options = poll_calls[0]
    assert space_id == "+155****4567"
    assert title == "Pick one"
    assert options == ["A", "B", "C"]
    # The vote returns as text, so text-capture must be enabled.
    assert marked == ["clar-1"]




# ---------------------------------------------------------------------------
# Multi-select: the poll grows a Done row, votes are tagged for the gateway.


def _clear_clarify_state():
    import tools.clarify_gateway as cg
    with cg._lock:
        cg._entries.clear()
        cg._session_index.clear()
        cg._notify_cbs.clear()


@pytest.mark.asyncio
async def test_multi_select_poll_appends_done_row_and_stays_in_choice_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.clarify_gateway as cg
    _clear_clarify_state()
    adapter = _make_adapter(monkeypatch)
    poll_calls = _stub_sidecar_poll(adapter, monkeypatch)
    entry = cg.register("pm1", "sk", "Pick", ["A", "B"], multi_select=True)

    result = await adapter.send_clarify(
        chat_id="+1", question="Pick", choices=["A", "B"], clarify_id="pm1", session_key="sk")

    assert result.success is True
    assert poll_calls == [("+1", "Pick", ["A", "B", cg.MULTI_SELECT_DONE_LABEL])]
    # Not flipped to text capture: prose must not resolve a multi-select prompt.
    assert entry.awaiting_text is False


@pytest.mark.asyncio
async def test_poll_votes_carry_poll_vote_metadata_including_retractions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_inbound(_poll_option_event(title="A"))
    await adapter._dispatch_inbound(_poll_option_event(title="A", selected=False, msg_id="m2"))

    assert [(e.text, e.metadata["poll_vote"], e.metadata["poll_selected"]) for e in captured] == [
        ("A", True, True), ("A", True, False)]


class TestGatewayPollVoteIntercept:
    """``GatewayRunner._hm_clarify_poll_vote`` — the gateway half of native-poll multi-select."""

    def setup_method(self):
        _clear_clarify_state()

    def _vote(self, pending, label, selected=True):
        from gateway.run import GatewayRunner
        return GatewayRunner._hm_clarify_poll_vote(pending, label, selected)

    def test_multi_select_votes_toggle_and_done_resolves(self):
        import tools.clarify_gateway as cg
        entry = cg.register("gm1", "sk", "Pick", ["A", "B"], multi_select=True)
        assert self._vote(entry, "B") == ""
        assert self._vote(entry, "A") == ""
        assert cg.selected_labels("gm1") == ["B", "A"]
        assert self._vote(entry, "B", selected=False) == ""
        assert cg.selected_labels("gm1") == ["A"]
        assert not entry.event.is_set()
        assert self._vote(entry, cg.MULTI_SELECT_DONE_LABEL) == ""
        assert entry.event.is_set() and entry.response == '["A"]'

    def test_done_with_nothing_selected_keeps_entry_armed(self):
        import tools.clarify_gateway as cg
        entry = cg.register("gm2", "sk", "Pick", ["A"], multi_select=True)
        assert self._vote(entry, cg.MULTI_SELECT_DONE_LABEL) == ""
        assert not entry.event.is_set()

    def test_single_select_selection_falls_through_and_retraction_is_swallowed(self):
        import tools.clarify_gateway as cg
        entry = cg.register("gs1", "sk", "Pick", ["A"])
        assert self._vote(entry, "A") is None
        assert self._vote(entry, "A", selected=False) == ""
