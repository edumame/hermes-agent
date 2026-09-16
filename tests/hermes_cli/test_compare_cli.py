"""Tests for the CLI /compare handler (hermes_cli/cli_model_switch_mixin.py::_cmd_compare).

The REPL half of the feature: parse ``--models``, print one side-result panel per answer as it
lands, then the summary; without ``--models`` open the model picker (the clarify checklist under
the prompt_toolkit app, a numbered stdin prompt on a plain TTY) and fall back to printing the
suggestion list where nothing can prompt. The comparison itself is stubbed — no model is called.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from cli import HermesCLI


def _fake_run_comparison(candidates, prompt, *, on_result=None, **_kw):
    from agent.model_compare import CompareResult
    results = []
    for i, cand in enumerate(candidates):
        ok = cand.model != "bad"
        r = CompareResult(cand.provider, cand.model, cand.display,
                          text="" if not ok else f"answer {cand.model} to {prompt}",
                          error=None if ok else "down", elapsed_s=1.0)
        results.append(r)
        if on_result is not None:
            on_result(r, i + 1, len(candidates))
    return results


def _cli(provider="anthropic", model="cur"):
    c = HermesCLI.__new__(HermesCLI)
    c.provider = provider
    c.model = model
    c.base_url = ""
    c.config = {}
    c._app = None
    c.console = None
    c._ensure_runtime_credentials = lambda: True
    return c


def _run(c, command, **patches):
    printed, panels = [], []
    with patch("cli._cprint", side_effect=lambda text: printed.append(text)), \
            patch("hermes_cli.cli_commands_mixin._print_side_result_panel",
                  side_effect=lambda cli, **kw: panels.append(kw)), \
            patch("agent.model_compare.run_comparison", _fake_run_comparison):
        assert c.process_command(command) is True
    return printed, panels


def test_compare_is_dispatched_to_cmd_compare():
    assert HermesCLI._slash_handler("compare") == ("_cmd_compare", True)


def test_no_prompt_prints_usage():
    printed, panels = _run(_cli(), "/compare")
    assert any("Usage: /compare" in line for line in printed)
    assert panels == []


def test_pinned_models_print_a_panel_per_answer_then_summary():
    printed, panels = _run(_cli(), "/compare --models a:m1,m2,a:bad why?")
    assert printed[0].startswith("  🔬 Comparing 3 models: a:m1, anthropic:m2, a:bad")
    assert [p["header_lines"][0] for p in panels] == [
        "  🔬 [1/3] a:m1", "  🔬 [2/3] anthropic:m2", "  🔬 [3/3] a:bad"]
    assert "answer m1 to why?" in panels[0]["body"]
    assert panels[2]["body"] == "❌ Failed: down"
    summary = printed[-1]
    assert "Comparison summary" in summary and "❌ a:bad" in summary and "✅ a:m1" in summary


def test_without_models_lists_suggestions_where_nothing_can_prompt():
    c = _cli()
    with patch.object(c, "_compare_suggestions", return_value=["anthropic:cur", "openrouter:x"]), \
            patch.object(c, "_compare_pick_models", return_value=None):
        printed, panels = _run(c, "/compare explain monads")
    assert panels == []
    assert any("--models provider:model" in line for line in printed)
    assert any(line.strip() == "anthropic:cur" for line in printed)
    assert any("Example: /compare --models anthropic:cur,openrouter:x" in line for line in printed)


def test_suggestions_come_from_explicitly_configured_providers():
    c = _cli(provider="anthropic", model="cur")
    c.config = {"providers": {"openrouter": {"api_key": "k"}}, "model_catalog": {"excluded_providers": ["nous"]}}
    rows = [{"slug": "openrouter", "models": ["m1", "m2"]}, {"slug": "moa", "models": ["p"]}]
    with patch("hermes_cli.inventory.compare_provider_rows", return_value=rows) as listing:
        assert c._compare_suggestions() == ["anthropic:cur", "openrouter:m1", "openrouter:m2"]
    ctx = listing.call_args.args[0]
    assert (ctx.current_provider, ctx.current_model) == ("anthropic", "cur")
    assert ctx.user_providers == {"openrouter": {"api_key": "k"}}
    assert ctx.excluded_providers == ["nous"]
    assert listing.call_args.kwargs == {}  # every model — the checklist scrolls


def test_suggestions_list_every_model_of_every_provider():
    c = _cli(provider="anthropic", model="cur")
    rows = [{"slug": "openrouter", "models": [f"m{i}" for i in range(20)]}, {"slug": "anthropic", "models": ["cur", "next"]}]
    with patch("hermes_cli.inventory.compare_provider_rows", return_value=rows):
        out = c._compare_suggestions()
    assert len(out) == 22 and out[0] == "anthropic:cur" and out[-1] == "anthropic:next"


def test_suggestions_survive_a_failed_listing():
    c = _cli(provider="anthropic", model="cur")
    with patch("hermes_cli.inventory.compare_provider_rows", side_effect=RuntimeError("offline")):
        assert c._compare_suggestions() == ["anthropic:cur"]


def test_missing_credentials_short_circuits():
    c = _cli()
    c._ensure_runtime_credentials = lambda: False
    printed, panels = _run(c, "/compare --models a:b hi")
    assert panels == [] and any("no valid credentials" in line for line in printed)


def test_picked_models_run_the_comparison():
    c = _cli()
    seen = {}

    def _pick(rows, **_kw):
        seen["rows"] = rows
        return ["anthropic:cur", "openrouter:x"]

    with patch.object(c, "_compare_suggestions", return_value=["anthropic:cur", "openrouter:x", "openrouter:y"]), \
            patch.object(c, "_compare_pick_models", side_effect=_pick):
        printed, panels = _run(c, "/compare explain monads")
    assert seen["rows"] == ["anthropic:cur", "openrouter:x", "openrouter:y"]
    assert printed[0].startswith("  🔬 Comparing 2 models: anthropic:cur, openrouter:x")
    assert [p["header_lines"][0] for p in panels] == ["  🔬 [1/2] anthropic:cur", "  🔬 [2/2] openrouter:x"]


def test_empty_pick_cancels_without_running():
    c = _cli()
    with patch.object(c, "_compare_suggestions", return_value=["anthropic:cur", "openrouter:x"]), \
            patch.object(c, "_compare_pick_models", return_value=[]):
        printed, panels = _run(c, "/compare explain monads")
    assert panels == []
    assert any("cancelled" in line for line in printed)


def test_resolve_compare_picks_maps_numbers_and_literals():
    from hermes_cli.cli_model_switch_mixin import _resolve_compare_picks
    rows = ["a:1", "b:2", "c:3"]
    assert _resolve_compare_picks("1, 3", rows) == ["a:1", "c:3"]
    assert _resolve_compare_picks("b:2, custom:x, 9, b:2", rows) == ["b:2", "custom:x"]
    assert _resolve_compare_picks("", rows) == []
    assert _resolve_compare_picks("[user did not respond within 10m]", rows) == []


def _picker_cli():
    c = _cli()
    c._app = object()
    c._paint_now = lambda: None
    c._ring_bell = lambda **_kw: None
    c._clarify_teardown = lambda: setattr(c, "_clarify_state", None)
    return c


def _pick_off_main_thread(c, rows):
    out = []
    t = threading.Thread(target=lambda: out.append(c._compare_pick_models(rows)))
    t.start()
    t.join(5)
    assert out, "picker did not return"
    return out[0]


def test_picker_under_app_is_a_multi_select_clarify_checklist():
    c = _picker_cli()
    seen = {}

    def _poll(response_queue, deadline_attr):
        seen["state"] = dict(c._clarify_state)
        seen["deadline"] = getattr(c, deadline_attr)
        return "anthropic:cur, openrouter:x"

    c._poll_modal_queue = _poll
    with patch("cli._cprint"):
        picked = _pick_off_main_thread(c, ["anthropic:cur", "openrouter:x", "openrouter:y"])
    assert picked == ["anthropic:cur", "openrouter:x"]
    assert seen["state"]["multi_select"] is True
    assert seen["state"]["choices"] == ["anthropic:cur", "openrouter:x", "openrouter:y"]
    assert seen["state"]["selected_indices"] == set()
    assert seen["deadline"] is not None


def test_picker_under_app_timeout_tears_down_and_cancels():
    from hermes_cli.cli_modal_mixin import _TIMED_OUT
    c = _picker_cli()
    c._poll_modal_queue = lambda _q, _attr: _TIMED_OUT
    printed = []
    with patch("cli._cprint", side_effect=lambda text: printed.append(text)):
        picked = _pick_off_main_thread(c, ["a:1", "b:2"])
    assert picked == []
    assert c._clarify_state is None
    assert any("timed out" in line for line in printed)


def test_picker_never_blocks_the_ui_thread():
    c = _picker_cli()
    c._poll_modal_queue = lambda _q, _attr: "a:1"
    assert c._compare_pick_models(["a:1", "b:2"]) is None  # main thread + live app → caller lists


def test_picker_on_a_plain_tty_prompts_a_numbered_list():
    c = _cli()
    printed = []
    c._prompt_text_input = lambda _prompt: "2, custom:z"
    with patch("cli._cprint", side_effect=lambda text: printed.append(text)), \
            patch("sys.stdin") as stdin:
        stdin.isatty.return_value = True
        assert c._compare_pick_models(["a:1", "b:2"]) == ["b:2", "custom:z"]
    assert any(line.strip() == "1. a:1" for line in printed)


def test_picker_with_piped_stdin_cannot_prompt():
    c = _cli()
    with patch("sys.stdin") as stdin:
        stdin.isatty.return_value = False
        assert c._compare_pick_models(["a:1", "b:2"]) is None
    assert c._compare_pick_models([]) is None


# ---------------------------------------------------------------------------
# Saved selection: the models of the last comparison carry over to the next /compare


def _saved(monkeypatch, tmp_path, labels=None):
    from agent.model_compare import save_candidates
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if labels:
        save_candidates(labels, tmp_path)


def test_pinned_models_are_remembered_for_the_next_compare(monkeypatch, tmp_path):
    from agent.model_compare import load_saved_candidates
    _saved(monkeypatch, tmp_path)
    _run(_cli(), "/compare --models a:m1,m2 why?")
    assert load_saved_candidates(tmp_path) == ["a:m1", "anthropic:m2"]


def test_picker_starts_with_the_saved_models_checked(monkeypatch, tmp_path):
    from agent.model_compare import load_saved_candidates
    _saved(monkeypatch, tmp_path, ["openrouter:x"])
    c = _cli()
    seen = {}

    def _pick(rows, *, preselected=None):
        seen["preselected"] = preselected
        return ["anthropic:cur", "openrouter:y"]

    with patch.object(c, "_compare_suggestions", return_value=["anthropic:cur", "openrouter:x", "openrouter:y"]), \
            patch.object(c, "_compare_pick_models", side_effect=_pick):
        printed, panels = _run(c, "/compare explain monads")
    assert seen["preselected"] == ["openrouter:x"]
    assert printed[0].startswith("  🔬 Comparing 2 models: anthropic:cur, openrouter:y")
    assert load_saved_candidates(tmp_path) == ["anthropic:cur", "openrouter:y"]  # the new pick replaces it


def test_saved_models_stand_in_where_nothing_can_prompt(monkeypatch, tmp_path):
    _saved(monkeypatch, tmp_path, ["a:m1", "b:m2"])
    c = _cli()
    with patch.object(c, "_compare_suggestions", return_value=["anthropic:cur"]), \
            patch.object(c, "_compare_pick_models", return_value=None):
        printed, panels = _run(c, "/compare explain monads")
    assert any("Using the last comparison's models: a:m1, b:m2" in line for line in printed)
    assert [p["header_lines"][0] for p in panels] == ["  🔬 [1/2] a:m1", "  🔬 [2/2] b:m2"]


def test_pick_flag_lists_instead_of_reusing_where_nothing_can_prompt(monkeypatch, tmp_path):
    _saved(monkeypatch, tmp_path, ["a:m1", "b:m2"])
    c = _cli()
    with patch.object(c, "_compare_suggestions", return_value=["anthropic:cur", "openrouter:x"]), \
            patch.object(c, "_compare_pick_models", return_value=None):
        printed, panels = _run(c, "/compare --pick explain monads")
    assert panels == []
    assert any("--models provider:model" in line for line in printed)
    assert not any("Using the last" in line for line in printed)


def test_cancelled_pick_keeps_the_previous_selection(monkeypatch, tmp_path):
    from agent.model_compare import load_saved_candidates
    _saved(monkeypatch, tmp_path, ["a:m1"])
    c = _cli()
    with patch.object(c, "_compare_suggestions", return_value=["anthropic:cur", "openrouter:x"]), \
            patch.object(c, "_compare_pick_models", return_value=[]):
        _run(c, "/compare explain monads")
    assert load_saved_candidates(tmp_path) == ["a:m1"]


def test_picker_under_app_prechecks_saved_rows_case_insensitively():
    c = _picker_cli()
    seen = {}

    def _poll(response_queue, deadline_attr):
        seen["state"] = dict(c._clarify_state)
        return "openrouter:x"

    c._poll_modal_queue = _poll
    with patch("cli._cprint"):
        out = []
        t = threading.Thread(target=lambda: out.append(c._compare_pick_models(
            ["anthropic:cur", "openrouter:x", "openrouter:y"], preselected=["OpenRouter:X", "openrouter:y", "gone:z"])))
        t.start()
        t.join(5)
    assert out == [["openrouter:x"]]
    assert seen["state"]["selected_indices"] == {1, 2}
    assert seen["state"]["selected"] == 1  # cursor rests on the first checked row
    assert "pre-checked" in seen["state"]["question"]


def test_picker_on_a_plain_tty_reuses_saved_rows_on_a_blank_answer():
    c = _cli()
    printed = []
    prompts = []

    def _ask(prompt):
        prompts.append(prompt)
        return "   "

    c._prompt_text_input = _ask
    with patch("cli._cprint", side_effect=lambda text: printed.append(text)), \
            patch("sys.stdin") as stdin:
        stdin.isatty.return_value = True
        assert c._compare_pick_models(["a:1", "b:2"], preselected=["b:2", "custom:z"]) == ["b:2", "custom:z"]
    assert any(line.strip() == "2. b:2  (last)" for line in printed)
    assert any("Last comparison: b:2, custom:z" in line for line in printed)
    assert "blank reuses the last" in prompts[0]


def test_picker_on_a_plain_tty_typed_answer_overrides_saved_rows():
    c = _cli()
    c._prompt_text_input = lambda _prompt: "1"
    with patch("cli._cprint"), patch("sys.stdin") as stdin:
        stdin.isatty.return_value = True
        assert c._compare_pick_models(["a:1", "b:2"], preselected=["b:2"]) == ["a:1"]
