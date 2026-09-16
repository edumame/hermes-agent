"""``compare.run`` / ``compare.options`` JSON-RPC handlers: the desktop Compare page's and the TUI
``/compare`` picker's door into ``agent/model_compare.py``.

Rebound onto server.py's globals at install time (``method_ctx.bind_module``), so bodies reference
server globals bare (``_ok``, ``_err``, ``_sessions``, ...). The desktop fires one request per
candidate so answers paint as they arrive; the handler also accepts several candidates for callers
that want a single round trip. Stateless: no session is touched.
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
    from agent.model_compare import suggest_candidate_labels
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
    return _ok(rid, {"choices": choices, "current": current})


def register(server) -> None:
    """Rebind this module's helpers + handlers onto ``server`` and register the handlers."""
    bind_module(globals(), server, skip=("_",))
