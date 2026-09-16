"""``/compare`` — one prompt, several models, every answer printed separately.

Bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``. The command asks which models
to compare through the gateway's clarify prompt in **multi-select** mode (button adapters render
toggle rows + Done, native-poll adapters a multi-vote poll, everything else a numbered list the
text-intercept parses as ``1, 3``), then fans the prompt out through ``agent/model_compare.py``
and posts each answer as it lands. ``--models a:b,c:d`` skips the poll. The live session is never
touched: no history rows, no model switch, no prompt-cache impact (same contract as ``/btw``).

The models of the last comparison are saved per profile (``agent.model_compare.save_candidates``):
a later bare ``/compare <prompt>`` reuses them without a poll — button rows cannot be pre-toggled
on a phone, so re-asking every time was the friction this removes — and ``--pick`` opens the
poll again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import List

from agent.model_compare import (
    SUGGEST_MAX_LABELS, SUGGEST_MODELS_PER_PROVIDER, parse_compare_args, parse_compare_options,
    suggest_candidate_labels,
)
from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py

# The poll offers ``agent.model_compare.SUGGEST_MAX_LABELS`` rows (Discord caps a view at 24 buttons,
# Telegram stacks rows; more than that is noise on a phone). The session's current model is row 1.
COMPARE_POLL_MAX_CHOICES = SUGGEST_MAX_LABELS
_COMPARE_POLL_MODELS_PER_PROVIDER = SUGGEST_MODELS_PER_PROVIDER
# A poll left unanswered releases the command well before the (hour-long) tool clarify default.
COMPARE_POLL_TIMEOUT_S = 600
_parse_compare_args = parse_compare_args


def _decode_poll_reply(reply: str) -> List[str]:
    """A multi-select clarify resolves to a JSON array of labels; a typed single label is a bare
    string; ``[...]`` sentinels (timeout, undeliverable) and empty replies mean no selection."""
    text = str(reply or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError:
            return []  # "[user did not respond within 10m]" and friends
        return [str(x).strip() for x in decoded if str(x).strip()] if isinstance(decoded, list) else []
    return [s.strip() for s in text.split(",") if s.strip()]


def _poll_choices(providers: List[dict], *, current_provider: str, current_model: str) -> List[str]:
    """``provider:model`` rows for the poll (see ``suggest_candidate_labels``)."""
    return suggest_candidate_labels(
        providers, current_provider=current_provider, current_model=current_model,
        limit=COMPARE_POLL_MAX_CHOICES, per_provider=_COMPARE_POLL_MODELS_PER_PROVIDER)


class GatewayCompareCommandsMixin:
    """``/compare`` handler."""

    def _compare_usage(self, event: MessageEvent) -> str:
        prefix = self._typed_command_prefix_for(event.source.platform)
        return (
            f"Usage: {prefix}compare <prompt>\n"
            f"       {prefix}compare --models openrouter:openai/gpt-5,anthropic:claude-opus-5 <prompt>\n"
            f"       {prefix}compare --pick <prompt>\n\n"
            "Sends the prompt to several models and prints every answer separately so you can compare "
            "them. Without --models you get a poll of models on your configured providers (select several, then Done). "
            "The models you picked are remembered: the next bare compare reuses them, --pick asks again."
        )

    async def _compare_listing(self, event: MessageEvent, source, profile_home) -> tuple[List[dict], str, str]:
        """Explicitly configured provider rows (``hermes_cli.inventory.compare_provider_rows``) + the
        session's current ``(provider, model)`` for the poll."""
        from gateway.run import _hermes_home
        from gateway.slash_commands_model import _ModelSwitchContext
        from hermes_cli.config import stringify_provider_map
        from hermes_cli.inventory import ConfigContext, compare_provider_rows

        session_key = self._session_key_for_source(source)
        ctx = _ModelSwitchContext(
            session_key=session_key, source=source,
            config_path=(profile_home or _hermes_home) / "config.yaml", persist_global=False)
        ctx.read_config()
        ctx.apply_override(getattr(self, "_session_model_overrides", {}).get(session_key, {}))
        try:
            current_model, rt = self._resolve_session_agent_runtime(source=source)
            current_provider = str(rt.get("provider") or ctx.current_provider or "")
        except Exception:
            current_model, current_provider = ctx.current_model, ctx.current_provider
        picker_ctx = ConfigContext(
            current_provider=str(ctx.current_provider or ""), current_model=str(ctx.current_model or ""),
            current_base_url=str(ctx.current_base_url or ""),
            user_providers=stringify_provider_map(ctx.user_provs), custom_providers=ctx.custom_provs,
            excluded_providers=list(ctx.excluded_provs or []))
        try:  # off-loop: listing can hit a stale-cache HTTP fetch
            providers = await asyncio.to_thread(
                compare_provider_rows, picker_ctx, max_models=_COMPARE_POLL_MODELS_PER_PROVIDER)
        except Exception as exc:
            logger.debug("/compare provider listing failed: %s", exc)
            providers = []
        return providers, str(current_provider or ""), str(current_model or "")

    async def _compare_poll(self, adapter, source, event: MessageEvent, choices: List[str]) -> List[str]:
        """Multi-select clarify poll → chosen ``provider:model`` labels (empty on timeout/cancel)."""
        from tools import clarify_gateway as clarify_mod

        session_key = self._session_key_for_source(source)
        clarify_id = uuid.uuid4().hex[:10]
        clarify_mod.register(
            clarify_id=clarify_id, session_key=session_key,
            question="Which models should answer? Pick one or more, then Done.",
            choices=choices, multi_select=True)
        try:
            sent = await adapter.send_clarify(
                chat_id=source.chat_id, question="Which models should answer? Pick one or more, then Done.",
                choices=choices, clarify_id=clarify_id, session_key=session_key,
                metadata=self._reply_metadata(event))
        except Exception as exc:
            logger.warning("/compare poll send failed: %s", exc)
            sent = None
        if sent is None or not getattr(sent, "success", False):
            clarify_mod.clear_session(session_key)
            return []
        timeout = min(COMPARE_POLL_TIMEOUT_S, max(1, int(clarify_mod.get_clarify_timeout() or COMPARE_POLL_TIMEOUT_S)))
        reply = await asyncio.to_thread(clarify_mod.wait_for_response, clarify_id, float(timeout))
        return _decode_poll_reply(reply or "")

    async def _handle_compare_command(self, event: MessageEvent) -> str:
        """Handle ``/compare [--models a:b,...] <prompt>``: poll (unless pinned), fan out, print."""
        from agent.model_compare import (
            MAX_COMPARE_CANDIDATES, format_result_message, format_summary_message, load_saved_candidates,
            parse_candidate_list, run_comparison, save_candidates,
        )

        args = parse_compare_options(event.get_command_args())
        specs, prompt = list(args.specs), args.prompt
        if not prompt:
            return self._compare_usage(event)
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        adapter = self._adapter_for_source(source)
        if adapter is None:
            return "❌ No adapter for this chat."
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            try:
                profile_home = self._resolve_profile_home_for_source(event.source)
            except Exception:
                profile_home = None
        reply_metadata = self._reply_metadata(event)
        from hermes_constants import get_hermes_home
        selection_home = profile_home or get_hermes_home()

        def _scoped(fn, *args, **kwargs):
            if profile_home is None:
                return fn(*args, **kwargs)
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(profile_home):
                return fn(*args, **kwargs)

        async def _run() -> None:
            try:
                providers, current_provider, current_model = await self._compare_listing(event, source, profile_home)
                saved = [] if (specs or args.pick) else load_saved_candidates(selection_home)
                reused = bool(saved)
                if specs:
                    labels = specs
                elif saved:
                    labels = saved
                else:
                    choices = _poll_choices(providers, current_provider=current_provider, current_model=current_model)
                    if len(choices) < 2:
                        await adapter.send(
                            source.chat_id,
                            "❌ Fewer than two models are available to compare. Add provider credentials "
                            "(hermes model) or pass --models provider:model,provider:model.",
                            metadata=reply_metadata)
                        return
                    labels = await self._compare_poll(adapter, source, event, choices)
                    if not labels:
                        await adapter.send(source.chat_id, "🟡 Comparison cancelled — no models were selected.",
                                           metadata=reply_metadata)
                        return
                candidates = parse_candidate_list(labels, default_provider=current_provider)
                if not candidates:
                    await adapter.send(source.chat_id, "❌ No valid models to compare.", metadata=reply_metadata)
                    return
                if len(candidates) > MAX_COMPARE_CANDIDATES:
                    candidates = candidates[:MAX_COMPARE_CANDIDATES]
                save_candidates(candidates, selection_home)  # the next bare /compare starts from these
                names = ", ".join(c.display for c in candidates)
                intro = (f"🔬 Comparing {len(candidates)} models (last selection): {names}\n"
                         f"Add --pick to choose different models. " if reused
                         else f"🔬 Comparing {len(candidates)} models: {names}\n")
                await adapter.send(
                    source.chat_id, f"{intro}Answers arrive as each model finishes…", metadata=reply_metadata)
                loop = asyncio.get_running_loop()
                total = len(candidates)

                def _on_result(result, done, _total) -> None:
                    text = format_result_message(result, index=done, total=total)
                    fut = asyncio.run_coroutine_threadsafe(
                        adapter.send(source.chat_id, text, metadata=reply_metadata), loop)
                    try:
                        fut.result(timeout=60)
                    except Exception as exc:  # pragma: no cover - delivery is best-effort
                        logger.warning("/compare answer delivery failed: %s", exc)

                results = await asyncio.to_thread(
                    _scoped, run_comparison, candidates, prompt, on_result=_on_result)
                await adapter.send(source.chat_id, format_summary_message(results), metadata=reply_metadata)
            except Exception as exc:
                logger.warning("/compare failed: %s", exc, exc_info=True)
                try:
                    await adapter.send(source.chat_id, f"❌ /compare failed: {exc}", metadata=reply_metadata)
                except Exception:
                    pass

        self._track_background_task(_run())
        # The poll / progress messages are the visible reply; an ack here would double-post.
        return ""
