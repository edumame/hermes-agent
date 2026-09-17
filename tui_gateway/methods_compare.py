"""``compare.run`` / ``compare.options`` / ``compare.save`` JSON-RPC handlers: the desktop Compare
page's and the TUI ``/compare`` picker's door into ``agent/model_compare.py``.

Rebound onto server.py's globals at install time (``method_ctx.bind_module``), so bodies reference
server globals bare (``_ok``, ``_err``, ``_sessions``, ...). The desktop fires one request per
candidate so answers paint as they arrive; the handler also accepts several candidates for callers
that want a single round trip. ``compare.run`` is stateless (no live session is touched);
``compare.save`` is how a finished comparison becomes a chat — a titled row in the profile's session
store the sidebar lists and ``session.resume`` opens like any other.
"""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

_COMPARE_MAX_TOKENS_CAP = 16384
_COMPARE_TIMEOUT_CAP_S = 600.0


def _compare_float(params: dict, key: str, default, *, low=None, high=None):
    """``float(params[key])`` clamped to ``[low, high]``; ``default`` for missing/unparsable."""
    raw = params.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


@method("compare.run")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Run ``prompt`` against ``candidates`` (``[{provider, model, label?}]`` or ``"provider:model"``
    strings) in parallel and return every answer (``results``, candidate order). Optional
    ``system`` (system prompt), ``max_tokens``, ``temperature``, ``timeout`` (seconds) and a live
    ``session_id`` whose transcript is lent as context (``include_history``)."""
    from agent.model_compare import MAX_COMPARE_CANDIDATES, candidates_from_dicts, run_comparison

    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return _err(rid, 4040, "prompt required")
    raw = params.get("candidates")
    if isinstance(raw, (str, dict)):
        raw = [raw]
    candidates = candidates_from_dicts(raw if isinstance(raw, list) else [])
    if not candidates:
        return _err(rid, 4041, f"candidates required (1-{MAX_COMPARE_CANDIDATES} of {{provider, model}})")
    history = None
    if params.get("include_history"):
        session = _sessions.get(params.get("session_id") or "")
        if session is not None:
            agent = session.get("agent")
            history = list(getattr(agent, "_session_messages", None) or session.get("history") or [])
    system_prompt = str(params.get("system") or "").strip() or None
    max_tokens = _int_param(params, "max_tokens", 2048)
    max_tokens = max(1, min(_COMPARE_MAX_TOKENS_CAP, max_tokens))
    temperature = _compare_float(params, "temperature", None, low=0.0, high=2.0)
    timeout = _compare_float(params, "timeout", 180.0, low=5.0, high=_COMPARE_TIMEOUT_CAP_S)
    try:
        results = run_comparison(
            candidates, prompt, system_prompt=system_prompt, history=history,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout)
    except ValueError as e:
        return _err(rid, 4040, str(e))
    except Exception as e:
        logger.warning("compare.run failed: %s", e)
        return _err(rid, 5040, f"comparison failed: {e}")
    return _ok(rid, {"prompt": prompt, "results": [r.to_dict() for r in results]})


@method("compare.options")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Models the TUI picker offers for a bare ``/compare <prompt>``: the session's current route
    first, then every model of every provider configured in this profile
    (``hermes_cli.inventory.compare_provider_rows`` + ``agent.model_compare.suggest_candidate_labels``,
    the same rows the CLI checklist shows). ``current`` is the session's ``provider:model``."""
    from agent.model_compare import load_saved_candidates, suggest_candidate_labels
    from hermes_cli.inventory import compare_provider_rows

    ctx = _model_picker_context(_session_agent(params))
    try:  # every model: the overlay scrolls and filters, so no per-provider cap
        providers = compare_provider_rows(ctx, refresh=bool(params.get("refresh")))
    except Exception as e:
        logger.debug("compare.options provider listing failed: %s", e)
        providers = []
    current_provider = str(ctx.current_provider or "")
    current_model = str(ctx.current_model or "")
    choices = suggest_candidate_labels(
        providers, current_provider=current_provider, current_model=current_model, limit=None, per_provider=None)
    current = f"{current_provider}:{current_model}" if current_provider and current_model else current_model
    # The last comparison's models: the overlay pre-checks them so Enter alone repeats the set.
    selected = load_saved_candidates()
    return _ok(rid, {"choices": choices, "current": current, "selected": selected})


@method("compare.remember")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Save ``candidates`` (``"provider:model"`` strings or ``{provider, model}`` dicts) as the
    selection the next ``/compare`` starts from (``agent.model_compare.save_candidates``). The TUI
    calls it once per comparison — ``compare.run`` is per candidate there, so it cannot know the
    whole set. Returns the stored labels; an empty list clears nothing (the old selection stays)."""
    from agent.model_compare import candidates_from_dicts, save_candidates

    raw = params.get("candidates")
    if isinstance(raw, (str, dict)):
        raw = [raw]
    candidates = candidates_from_dicts(raw if isinstance(raw, list) else [])
    return _ok(rid, {"models": save_candidates(candidates)})


@method("compare.selection")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """The profile's saved comparison set — ONE list shared by every surface, so the desktop page,
    the TUI picker and the messaging ``/compare`` all start from the same models. Without ``models``
    it reads; with ``models`` (``"provider:model"`` strings or ``{provider, model}`` dicts, an empty
    list included) it replaces the selection and returns what was stored. Unlike ``compare.remember``
    an empty list clears: the desktop edits the list in place, so "remove the last model" must stick."""
    from agent.model_compare import candidates_from_dicts, load_saved_candidates, save_candidates

    raw = params.get("models")
    if isinstance(raw, list):
        candidates = candidates_from_dicts(raw)
        return _ok(rid, {"models": save_candidates(candidates, allow_empty=True)})
    return _ok(rid, {"models": load_saved_candidates()})


@method("compare.save")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Persist a finished comparison as a chat in ``params['profile']``'s session store: ``prompt`` as the
    user turn, ``results`` (the rows ``compare.run`` returned, in candidate order) as one assistant turn
    (``agent.model_compare.comparison_chat_messages``). The row is a plain stored session — no runtime,
    no agent build — that the sidebar lists under ``title`` (default ``Compare: <prompt>``) and that
    opens through ``session.resume`` like any other chat, so the user can carry on from the answers.
    Returns ``stored_session_id`` and the title actually used (a duplicate title gets a suffix)."""
    from agent.model_compare import persist_comparison_chat, results_from_dicts

    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return _err(rid, 4040, "prompt required")
    raw = params.get("results")
    results = results_from_dicts(raw if isinstance(raw, list) else [])
    if not results:
        return _err(rid, 4042, "results required (the rows compare.run returned)")
    try:
        with _profile_db(params) as db:
            if db is None:
                return _err(rid, 5041, "session store unavailable")
            key, title = persist_comparison_chat(
                db, prompt, results, session_id=_new_session_key(), source=_resolve_session_platform(),
                profile_name=_response_profile_name(params.get("profile")), title=str(params.get("title") or ""))
    except Exception as e:
        logger.warning("compare.save failed: %s", e)
        return _err(rid, 5041, f"could not save comparison: {e}")
    return _ok(rid, {"stored_session_id": key, "title": title, "message_count": 2})


@method("compare.branch")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Branch a saved comparison off ONE of its models: a child chat of ``session_id`` (the compare
    chat's stored id) seeded with ``prompt`` and that model's answer (``result``: one row as the saved
    turn carries it), pinned to that model — the row stores ``model``/``model_config`` exactly as a
    session the user switched to that model would, so resume continues on it. No runtime is built;
    the client opens the returned ``stored_session_id`` like any chat."""
    from agent.model_compare import branch_chat_messages, branch_chat_title, results_from_dicts

    parent = str(params.get("session_id") or "").strip()
    if not parent:
        return _err(rid, 4040, "session_id required (the compare chat to branch from)")
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return _err(rid, 4040, "prompt required")
    raw = params.get("result")
    results = results_from_dicts([raw] if isinstance(raw, dict) else [])
    result = results[0] if results else None
    if result is None or not result.ok or not result.text.strip():
        return _err(rid, 4043, "result required: the answered row to branch from")
    title = str(params.get("title") or "").strip() or branch_chat_title(prompt, result)
    messages = branch_chat_messages(prompt, result)
    try:
        with _profile_db(params) as db:
            if db is None:
                return _err(rid, 5041, "session store unavailable")
            if db.get_session(parent) is None:
                return _err(rid, 4001, "compare chat not found")
            key = _new_session_key()
            model_config = {"model": result.model, "_branched_from": parent}
            if result.provider:
                model_config["provider"] = result.provider
            db.create_session(key, source=_resolve_session_platform(), model=result.model, model_config=model_config,
                              parent_session_id=parent,
                              profile_name=_response_profile_name(params.get("profile")))
            db.append_messages_batch(key, messages)
            try:
                db.set_session_title(key, title)
            except ValueError:
                title = f"{title} ({key[-6:]})"
                db.set_session_title(key, title)
    except Exception as e:
        logger.warning("compare.branch failed: %s", e)
        return _err(rid, 5041, f"could not branch the comparison: {e}")
    return _ok(rid, {"stored_session_id": key, "title": title, "model": result.model, "provider": result.provider,
                     "parent_session_id": parent})


def register(server) -> None:
    """Rebind this module's helpers + handlers onto ``server`` and register the handlers."""
    bind_module(globals(), server, skip=("_",))
