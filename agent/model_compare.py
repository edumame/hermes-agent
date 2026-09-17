"""Multi-model comparison: one prompt, several candidate models, every answer kept separate.

Unlike Mixture of Agents (``agent/moa_loop.py``), which fans a prompt out to advisors and then
aggregates, a comparison never merges: each candidate answers the user directly and the caller
renders the answers side by side (desktop Compare page, ``/compare`` on messaging platforms).
Candidates are plain ``provider:model`` routes resolved through the same per-profile runtime
cache the MoA advisors use, so anything the model picker offers can be compared. Calls are
stateless ``call_llm`` requests: no tools, no session mutation, no prompt-cache impact.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

# Fan-out cap: a comparison is interactive, so keep it bounded for provider rate limits.
MAX_COMPARE_CANDIDATES = 8
_MAX_WORKERS = 4
_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_TIMEOUT_S = 180.0
_POLL_INTERVAL_S = 0.25

DEFAULT_SYSTEM_PROMPT = (
    "You are one of several AI models answering the same prompt so the user can compare the "
    "answers. Answer the prompt directly and completely on your own; you have no tools and cannot "
    "run code, browse, or read files. Do not mention the comparison or the other models."
)


@dataclass(frozen=True)
class CompareCandidate:
    """One model route to compare. ``label`` is what surfaces show (defaults to ``provider:model``)."""
    provider: str
    model: str
    label: str = ""

    @property
    def display(self) -> str:
        return self.label or (f"{self.provider}:{self.model}" if self.provider else self.model)

    def as_slot(self) -> Dict[str, str]:
        """MoA-slot shape so ``agent.moa_loop._slot_runtime`` resolves credentials for us."""
        return {"provider": self.provider, "model": self.model}


@dataclass
class CompareResult:
    """One candidate's answer (or failure). ``elapsed_s`` is wall-clock for that call only."""
    provider: str
    model: str
    label: str
    text: str = ""
    error: Optional[str] = None
    elapsed_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_status: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe wire shape. ``cost_usd`` arrives from usage pricing as a ``Decimal``, which
        ``json.dumps`` rejects — the desktop's websocket reply silently died on it while the CLI,
        which only formats the cost as text, was fine. Every number here is a plain float/int."""
        data = asdict(self)
        data["ok"] = self.ok
        data["cost_usd"] = None if self.cost_usd is None else float(self.cost_usd)
        data["elapsed_s"] = float(self.elapsed_s)
        return data


_MODELS_FLAG_RE = re.compile(r"(?:^|\s)--models?(?:=|\s+)(\S+)")
# ``--pick`` (alias ``--choose``): open the model picker even when a saved selection exists.
_PICK_FLAG_RE = re.compile(r"(?:^|\s)--(?:pick|choose)(?=\s|$)")

# Model rows the surfaces suggest/poll: the current route first, then a few per provider.
SUGGEST_MAX_LABELS = 8
SUGGEST_MODELS_PER_PROVIDER = 3


@dataclass(frozen=True)
class CompareArgs:
    """Parsed ``/compare`` text: pinned ``--models`` specs, the prompt, and whether ``--pick``
    asked for the picker despite a saved selection."""
    specs: List[str]
    prompt: str
    pick: bool = False


def parse_compare_options(raw: str) -> CompareArgs:
    """``[--models a:b,c:d] [--pick] <prompt>``. Either flag may sit anywhere in the text;
    surrounding whitespace collapses."""
    text = str(raw or "").strip()
    pick = bool(_PICK_FLAG_RE.search(text))
    if pick:
        text = " ".join(_PICK_FLAG_RE.sub(" ", text).split())
    match = _MODELS_FLAG_RE.search(text)
    if not match:
        return CompareArgs([], text, pick)
    specs = [s.strip() for s in match.group(1).split(",") if s.strip()]
    prompt = " ".join((text[: match.start()] + " " + text[match.end():]).split())
    return CompareArgs(specs, prompt, pick)


def parse_compare_args(raw: str) -> tuple[List[str], str]:
    """``[--models a:b,c:d] <prompt>`` → ``(specs, prompt)``; specs empty without the flag.
    ``--pick`` is stripped; callers that honour it use ``parse_compare_options``."""
    args = parse_compare_options(raw)
    return args.specs, args.prompt


# ─── Saved selection ─────────────────────────────────────────────────────────
# The models picked for the last comparison persist per profile so the next ``/compare`` does
# not start from a blank checklist: the CLI/TUI pickers pre-check them, the gateway (whose poll
# buttons cannot be pre-toggled) reuses them outright until ``--pick`` or ``--models`` says
# otherwise. The desktop Compare page keeps its own list in plugin storage.
COMPARE_SELECTION_FILE = "compare_models.json"


def compare_selection_path(home: Union[str, Path, None] = None) -> Path:
    """``<profile home>/compare_models.json``; ``home`` defaults to the active Hermes home."""
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return Path(home) / COMPARE_SELECTION_FILE


def normalize_candidate_labels(labels: Iterable[Any]) -> List[str]:
    """``provider:model`` strings (or ``CompareCandidate`` rows) → deduped labels, capped at the
    fan-out limit. Dedupe is case-insensitive; the first spelling wins."""
    out: List[str] = []
    seen: set = set()
    for item in labels or ():
        if isinstance(item, CompareCandidate):
            label = f"{item.provider}:{item.model}" if item.provider else item.model
        else:
            label = str(item or "").strip()
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= MAX_COMPARE_CANDIDATES:
            break
    return out


def load_saved_candidates(home: Union[str, Path, None] = None) -> List[str]:
    """Labels saved by the last comparison, ``[]`` when none were saved or the file is unreadable."""
    from utils import read_json_or_empty
    try:
        data = read_json_or_empty(compare_selection_path(home))
    except Exception:
        return []
    models = data.get("models")
    return normalize_candidate_labels(models) if isinstance(models, list) else []


def save_candidates(labels: Iterable[Any], home: Union[str, Path, None] = None, *,
                    allow_empty: bool = False) -> List[str]:
    """Persist ``labels`` as the selection the next comparison starts from; returns what was
    stored. An empty selection is not written (a cancelled pick keeps the previous one) unless
    ``allow_empty`` — a surface that edits the list in place (the desktop Compare page) clears it
    on purpose. Write failures are logged, never raised — remembering models must not break the
    comparison."""
    normalized = normalize_candidate_labels(labels)
    if not normalized and not allow_empty:
        return []
    try:
        from utils import atomic_json_write
        path = compare_selection_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"models": normalized})
    except Exception as exc:  # pragma: no cover - disk trouble is not a comparison failure
        logger.debug("could not save compare model selection: %s", exc)
    return normalized


def suggest_candidate_labels(
    providers: Iterable[dict], *, current_provider: str = "", current_model: str = "",
    limit: Optional[int] = SUGGEST_MAX_LABELS, per_provider: Optional[int] = SUGGEST_MODELS_PER_PROVIDER,
) -> List[str]:
    """``provider:model`` rows to offer: the current route first, then up to ``per_provider``
    models per provider row (``hermes_cli.model_switch.list_authenticated_providers`` shape)
    until ``limit``. ``None`` for either means no cap (the scrolling CLI/TUI checklists list every
    model; the chat poll keeps the defaults — a button grid cannot). The virtual ``moa`` provider
    is skipped — a preset aggregates, a comparison wants raw candidates."""
    rows: List[str] = []
    seen: set = set()

    def _add(provider: str, model: str) -> None:
        key = (str(provider).lower(), str(model).lower())
        if not provider or not model or key in seen or (limit is not None and len(rows) >= limit):
            return
        seen.add(key)
        rows.append(f"{provider}:{model}")

    if current_model:
        _add(current_provider or "openrouter", current_model)
    for p in providers or ():
        slug = str(p.get("slug") or "").strip()
        if slug.lower() == "moa":
            continue
        models = list(p.get("models") or [])
        for model in (models if per_provider is None else models[:per_provider]):
            _add(slug, str(model))
    return rows


def parse_candidate(spec: str, *, default_provider: str = "") -> Optional[CompareCandidate]:
    """``provider:model`` / ``provider/model``-free spec → candidate. A bare model name takes
    ``default_provider``. Returns None for an empty spec."""
    raw = str(spec or "").strip()
    if not raw:
        return None
    provider, sep, model = raw.partition(":")
    if not sep or not model.strip():
        return CompareCandidate(provider=default_provider.strip(), model=raw)
    return CompareCandidate(provider=provider.strip(), model=model.strip())


def parse_candidate_list(specs: Iterable[str], *, default_provider: str = "") -> List[CompareCandidate]:
    """Parse and dedupe candidate specs (order preserved), capped at ``MAX_COMPARE_CANDIDATES``."""
    seen: set = set()
    out: List[CompareCandidate] = []
    for spec in specs:
        cand = parse_candidate(spec, default_provider=default_provider)
        if cand is None:
            continue
        key = (cand.provider.lower(), cand.model.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
        if len(out) >= MAX_COMPARE_CANDIDATES:
            break
    return out


def candidates_from_dicts(items: Iterable[Any]) -> List[CompareCandidate]:
    """``[{"provider": ..., "model": ..., "label"?: ...}]`` (RPC shape) → candidates, deduped."""
    seen: set = set()
    out: List[CompareCandidate] = []
    for item in items or ():
        if isinstance(item, str):
            cand = parse_candidate(item)
        elif isinstance(item, dict):
            model = str(item.get("model") or "").strip()
            if not model:
                continue
            cand = CompareCandidate(
                provider=str(item.get("provider") or "").strip(), model=model,
                label=str(item.get("label") or "").strip())
        else:
            continue
        if cand is None:
            continue
        key = (cand.provider.lower(), cand.model.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
        if len(out) >= MAX_COMPARE_CANDIDATES:
            break
    return out


def build_messages(prompt: str, *, system_prompt: Optional[str] = None,
                   history: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Request messages: system (default advisory text) + optional transcript context + prompt.
    ``history`` rows are copied verbatim only for plain user/assistant text so every provider
    accepts them (tool rows and mid-turn tails are dropped)."""
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT}]
    last_role = "system"
    for row in history or ():
        role = row.get("role") if isinstance(row, dict) else None
        content = row.get("content") if isinstance(row, dict) else None
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if role == last_role:
            messages[-1] = {"role": role, "content": f"{messages[-1]['content']}\n\n{content}"}
        else:
            messages.append({"role": role, "content": content})
        last_role = role
    if last_role == "user":
        messages[-1] = {"role": "user", "content": f"{messages[-1]['content']}\n\n{prompt}"}
    else:
        messages.append({"role": "user", "content": prompt})
    return messages


def run_candidate(
    candidate: CompareCandidate, messages: List[Dict[str, Any]], *,
    max_tokens: int = _DEFAULT_MAX_TOKENS, temperature: Optional[float] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> CompareResult:
    """Call one candidate; never raises (a failure becomes ``error``)."""
    from agent.moa_loop import _extract_text, _price_reference_response, _slot_runtime

    slot = candidate.as_slot()
    result = CompareResult(provider=candidate.provider, model=candidate.model, label=candidate.display)
    started = time.monotonic()
    try:
        runtime = _slot_runtime(slot)
        from agent.auxiliary_client import call_llm
        response = call_llm(
            task="model_compare", messages=list(messages), max_tokens=max_tokens,
            temperature=temperature, timeout=timeout, **runtime)
        result.text = _extract_text(response) or ""
        if not result.text.strip():
            result.error = "empty response"
        usage, cost, status, _source = _price_reference_response(response, slot, runtime)
        result.input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        result.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        result.cost_usd = cost
        result.cost_status = status
        result.provider = str(runtime.get("provider") or candidate.provider)
    except Exception as exc:  # pragma: no cover - provider failures are data, not crashes
        logger.warning("model compare candidate %s failed: %s", candidate.display, exc)
        result.error = str(exc) or exc.__class__.__name__
    result.elapsed_s = round(time.monotonic() - started, 2)
    return result


def run_comparison(
    candidates: List[CompareCandidate], prompt: str, *,
    system_prompt: Optional[str] = None, history: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS, temperature: Optional[float] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    on_result: Optional[Callable[[CompareResult, int, int], None]] = None,
) -> List[CompareResult]:
    """Fan ``prompt`` out to every candidate in parallel; results come back in candidate order.
    ``on_result(result, done, total)`` fires on the calling thread as each candidate finishes
    (completion order) so a surface can print answers as they arrive."""
    if not candidates:
        return []
    if not str(prompt or "").strip():
        raise ValueError("compare prompt is empty")
    candidates = list(candidates)[:MAX_COMPARE_CANDIDATES]
    messages = build_messages(prompt, system_prompt=system_prompt, history=history)
    total = len(candidates)
    results: List[Optional[CompareResult]] = [None] * total
    from tools.thread_context import propagate_context_to_thread
    executor = ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, total), thread_name_prefix="model-compare")
    futures = {}
    try:
        for idx, cand in enumerate(candidates):
            fut = executor.submit(
                propagate_context_to_thread(run_candidate), cand, messages,
                max_tokens=max_tokens, temperature=temperature, timeout=timeout)
            futures[fut] = idx
        pending = set(futures)
        done_count = 0
        while pending:
            done, pending = _futures_wait(pending, timeout=_POLL_INTERVAL_S)
            for fut in done:
                idx = futures[fut]
                results[idx] = fut.result()
                done_count += 1
                if on_result is not None:
                    try:
                        on_result(results[idx], done_count, total)
                    except Exception:  # pragma: no cover - display must never break the run
                        logger.debug("compare on_result callback failed", exc_info=True)
    finally:
        executor.shutdown(wait=True)
    return [r for r in results if r is not None]


def format_cost(cost_usd: Optional[float]) -> str:
    if cost_usd is None:
        return "n/a"
    if cost_usd == 0:
        return "$0"
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    return f"${cost_usd:.2f}"


def format_result_message(result: CompareResult, *, index: int, total: int,
                          max_chars: Optional[int] = 3500) -> str:
    """Chat-ready rendering of one answer: a labelled header, the answer, and a stats footer.
    ``max_chars=None`` never truncates (a saved transcript keeps the whole answer)."""
    header = f"🔬 **[{index}/{total}] {result.label}**"
    if result.error:
        return f"{header}\n\n❌ Failed: {result.error}\n\n_{result.elapsed_s:.1f}s_"
    body = result.text.strip()
    if max_chars is not None and len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n\n… _(truncated)_"
    footer = f"_{result.elapsed_s:.1f}s · {result.input_tokens}→{result.output_tokens} tokens · {format_cost(result.cost_usd)}_"
    return f"{header}\n\n{body}\n\n{footer}"


def format_summary_message(results: List[CompareResult]) -> str:
    """Closing scoreboard for a comparison: one line per candidate, fastest first."""
    if not results:
        return "📊 Comparison finished with no results."
    lines = ["📊 **Comparison summary**"]
    for r in sorted(results, key=lambda r: (not r.ok, r.elapsed_s)):
        status = "✅" if r.ok else "❌"
        stats = f"{r.elapsed_s:.1f}s · {r.output_tokens} out tokens · {format_cost(r.cost_usd)}" if r.ok else (r.error or "failed")
        lines.append(f"{status} {r.label} — {stats}")
    return "\n".join(lines)


def format_comparison_text(results: List[CompareResult], *, max_chars: Optional[int] = 3500) -> str:
    """One text block for a single-message surface (TUI slash output): every answer in candidate
    order, then the summary scoreboard."""
    total = len(results)
    parts = [format_result_message(r, index=i + 1, total=total, max_chars=max_chars) for i, r in enumerate(results)]
    parts.append(format_summary_message(results))
    return "\n\n".join(parts)


# ── Saved comparisons (a finished run persisted as a chat) ───────────────────────────────────

COMPARE_CHAT_TITLE_PREFIX = "Compare: "
_CHAT_TITLE_MAX_CHARS = 72


def results_from_dicts(items: Iterable[Any]) -> List[CompareResult]:
    """Wire result rows (the ``CompareResult.to_dict`` shape ``compare.run`` returns, so a client can
    hand back exactly what it was given) as results. A row naming only its ``label`` (a client's own
    "request failed" row for a model the backend never answered for) takes provider/model from it; rows
    naming neither are dropped. A row is a failure when it carries an ``error`` or says ``ok: false`` —
    an empty error then reads "failed"."""
    out: List[CompareResult] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        provider = str(item.get("provider") or "").strip()
        label = str(item.get("label") or "").strip()
        if not model:
            parsed = parse_candidate(label) if label else None
            if parsed is None:
                continue
            provider, model = parsed.provider, parsed.model
        label = label or CompareCandidate(provider=provider, model=model).display
        error = item.get("error")
        error = str(error).strip() if error is not None and str(error).strip() else None
        if error is None and item.get("ok") is False:
            error = "failed"

        def _num(key: str, cast, default):
            try:
                value = item.get(key)
                return default if value is None else cast(value)
            except (TypeError, ValueError):
                return default

        out.append(CompareResult(
            provider=provider, model=model, label=label, text=str(item.get("text") or ""), error=error,
            elapsed_s=_num("elapsed_s", float, 0.0), input_tokens=_num("input_tokens", int, 0),
            output_tokens=_num("output_tokens", int, 0), cost_usd=_num("cost_usd", float, None),
            cost_status=(str(item["cost_status"]) if item.get("cost_status") is not None else None)))
    return out


def comparison_chat_title(prompt: str) -> str:
    """``Compare: <prompt>`` on one line, trimmed to a sidebar-sized title."""
    text = " ".join(str(prompt or "").split())
    budget = _CHAT_TITLE_MAX_CHARS - len(COMPARE_CHAT_TITLE_PREFIX)
    if len(text) > budget:
        text = text[:budget - 1].rstrip() + "…"
    return f"{COMPARE_CHAT_TITLE_PREFIX}{text}"


COMPARE_DISPLAY_KIND = "compare"


def comparison_display_metadata(prompt: str, results: List[CompareResult]) -> Dict[str, Any]:
    """Presentation payload stored beside the assistant turn (``display_metadata``): the structured
    rows a renderer paints as side-by-side cards with a "branch with this model" action, instead of
    the markdown it would otherwise show. The markdown stays the model-facing ``content``."""
    return {"kind": COMPARE_DISPLAY_KIND, "version": 1, "prompt": str(prompt or "").strip(),
            "results": [r.to_dict() for r in results]}


def comparison_chat_messages(prompt: str, results: List[CompareResult]) -> List[Dict[str, Any]]:
    """The transcript a saved comparison becomes: the prompt as the user turn, then every answer in
    candidate order plus the scoreboard as ONE assistant turn (untruncated). One assistant turn, not one
    per model, so the chat stays a valid alternating transcript when the user carries on in it. The
    assistant turn is typed ``display_kind="compare"`` and carries the structured rows, so a client
    that knows the kind renders cards; one that does not falls back to the markdown."""
    return [
        {"role": "user", "content": str(prompt or "").strip()},
        {"role": "assistant", "content": format_comparison_text(results, max_chars=None),
         "display_kind": COMPARE_DISPLAY_KIND, "display_metadata": comparison_display_metadata(prompt, results)},
    ]


def persist_comparison_chat(db, prompt: str, results: List[CompareResult], *, session_id: str, source: str,
                            profile_name: Optional[str] = None, title: Optional[str] = None) -> tuple[str, str]:
    """Write a finished comparison into ``db`` (a ``SessionDB``) as its own chat — the prompt as the user
    turn, every answer plus the scoreboard as one typed assistant turn (``comparison_chat_messages``) —
    under ``title`` (default ``Compare: <prompt>``; a duplicate title gets a ``(<id tail>)`` suffix, so
    the same prompt compared twice keeps both chats). ONE writer for every surface: the desktop page
    and the TUI picker go through ``compare.save``, the messaging ``/compare`` calls this directly, so a
    Telegram run lands in the same store the desktop sidebar lists. Returns ``(session_id, title)``;
    store errors propagate (callers decide whether a failed save is fatal)."""
    title = (title or "").strip() or comparison_chat_title(prompt)
    extra = {"profile_name": profile_name} if profile_name else {}
    db.create_session(session_id, source=source, **extra)
    db.append_messages_batch(session_id, comparison_chat_messages(prompt, results))
    try:
        db.set_session_title(session_id, title)
    except ValueError:
        title = f"{title} ({session_id[-6:]})"
        db.set_session_title(session_id, title)
    return session_id, title


def branch_chat_messages(prompt: str, result: CompareResult) -> List[Dict[str, str]]:
    """The transcript a branch off ONE compared model starts from: the prompt, then that model's own
    answer as the assistant turn — the chat then continues on that model as if it had been asked alone."""
    return [
        {"role": "user", "content": str(prompt or "").strip()},
        {"role": "assistant", "content": result.text.strip()},
    ]


def branch_chat_title(prompt: str, result: CompareResult) -> str:
    """``<model> · <prompt>`` trimmed to a sidebar-sized title."""
    text = " ".join(str(prompt or "").split())
    prefix = f"{result.model} · "
    budget = _CHAT_TITLE_MAX_CHARS - len(prefix)
    if budget < 12:
        prefix = prefix[:_CHAT_TITLE_MAX_CHARS - 13].rstrip() + "… · "
        budget = _CHAT_TITLE_MAX_CHARS - len(prefix)
    if len(text) > budget:
        text = text[:budget - 1].rstrip() + "…"
    return f"{prefix}{text}"
