"""The clarify panel scrolls a choice list longer than the viewport (hermes_cli/cli_tui_mixin.py).

The /compare checklist offers every model on the configured providers — dozens of rows — and the
panel used to render them all, so prompt_toolkit's HSplit clipped the tail (and the Other row)
off-screen. Now a window of rows follows the cursor with "N more" markers above and below.
"""

from __future__ import annotations

from unittest.mock import patch

from cli import HermesCLI
from hermes_cli.cli_tui_mixin import _window_choice_rows


def _cli(choices, selected, *, freetext=False):
    c = HermesCLI.__new__(HermesCLI)
    c._clarify_state = {"question": "Which models should answer?", "choices": list(choices), "selected": selected,
                        "multi_select": True, "selected_indices": {0}, "response_queue": None}
    c._clarify_freetext = freetext
    return c


def _lines(fragments):
    return "".join(text for _style, text in fragments).splitlines()


def test_window_choice_rows_follows_the_cursor_and_counts_choices():
    rows = [(i, f"row {i}") for i in range(50)]
    visible, above, below = _window_choice_rows(rows, 25, 10)
    assert [i for i, _ in visible] == list(range(20, 30)) and (above, below) == (20, 20)
    visible, above, below = _window_choice_rows(rows, 0, 10)
    assert visible[0][0] == 0 and (above, below) == (0, 40)
    visible, above, below = _window_choice_rows(rows, 49, 10)
    assert visible[-1][0] == 49 and (above, below) == (40, 0)
    assert _window_choice_rows(rows, 3, 100) == (rows, 0, 0)
    # A wrapped choice spans several rows but counts once.
    wrapped = [(0, "a"), (1, "b1"), (1, "b2"), (2, "c"), (3, "d")]
    assert _window_choice_rows(wrapped, 3, 2) == ([(2, "c"), (3, "d")], 2, 0)


def test_long_checklist_scrolls_around_the_cursor():
    choices = [f"openrouter:model-{i:02d}" for i in range(60)]
    with patch("hermes_cli.cli_tui_mixin._term_rows", return_value=24):
        lines = _lines(_cli(choices, 30)._get_clarify_display_fragments())
    body = "\n".join(lines)
    assert "model-30" in body and "model-00" not in body and "model-59" not in body
    assert "↑" in body and "more" in body and "↓" in body
    assert "Other" in body  # the mandatory rows still render
    assert len(lines) <= 24  # nothing for HSplit to clip


def test_short_checklist_has_no_scroll_markers():
    with patch("hermes_cli.cli_tui_mixin._term_rows", return_value=40):
        lines = _lines(_cli(["a:x", "b:y", "c:z"], 1)._get_clarify_display_fragments())
    body = "\n".join(lines)
    assert "a:x" in body and "c:z" in body and "more" not in body


def test_cursor_on_other_shows_the_tail_of_the_list():
    choices = [f"p:m{i}" for i in range(40)]
    with patch("hermes_cli.cli_tui_mixin._term_rows", return_value=20):
        lines = _lines(_cli(choices, 40)._get_clarify_display_fragments())
    body = "\n".join(lines)
    assert "p:m39" in body and "p:m0 " not in body and "Other" in body
