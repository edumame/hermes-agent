"""Tests for agent/model_compare.py — the multi-model comparison core.

Covers candidate parsing/dedupe, message assembly from a transcript snapshot, the parallel
fan-out contract (results in candidate order, ``on_result`` in completion order, one failure
never sinks the others), and the chat renderings. ``call_llm`` and runtime resolution are
stubbed: a comparison is a set of stateless auxiliary calls, so nothing here touches a session.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import model_compare as mc


class TestCandidates:
    def test_parse_provider_model(self):
        cand = mc.parse_candidate("openrouter:openai/gpt-5")
        assert cand == mc.CompareCandidate(provider="openrouter", model="openai/gpt-5")
        assert cand.display == "openrouter:openai/gpt-5"

    def test_bare_model_takes_default_provider(self):
        cand = mc.parse_candidate("gpt-5", default_provider="openai")
        assert cand.provider == "openai" and cand.model == "gpt-5"

    def test_empty_spec_is_none(self):
        assert mc.parse_candidate("   ") is None

    def test_list_dedupes_case_insensitively_and_caps(self):
        specs = ["a:M", "a:m", "b:x"] + [f"p{i}:m" for i in range(20)]
        out = mc.parse_candidate_list(specs)
        assert [c.display for c in out[:2]] == ["a:M", "b:x"]
        assert len(out) == mc.MAX_COMPARE_CANDIDATES

    def test_candidates_from_dicts_accepts_rpc_shape_and_strings(self):
        out = mc.candidates_from_dicts([
            {"provider": "anthropic", "model": "claude", "label": "Claude"},
            "openrouter:x", {"model": ""}, 42, {"provider": "anthropic", "model": "claude"},
        ])
        assert [(c.provider, c.model, c.display) for c in out] == [
            ("anthropic", "claude", "Claude"), ("openrouter", "x", "openrouter:x")]


class TestArgs:
    def test_pick_flag_is_lifted_out_anywhere(self):
        assert mc.parse_compare_options("--pick explain monads") == mc.CompareArgs([], "explain monads", True)
        assert mc.parse_compare_options("explain --choose monads") == mc.CompareArgs([], "explain monads", True)
        assert mc.parse_compare_options("--pick --models a:b hi") == mc.CompareArgs(["a:b"], "hi", True)
        assert mc.parse_compare_options("picky --pickle") == mc.CompareArgs([], "picky --pickle", False)

    def test_tuple_form_strips_pick(self):
        assert mc.parse_compare_args("--pick --models a:b hi") == (["a:b"], "hi")


class TestSavedSelection:
    def test_roundtrip_dedupes_and_caps(self, tmp_path):
        assert mc.load_saved_candidates(tmp_path) == []
        labels = ["a:M", "a:m", " b:x ", ""] + [f"p{i}:m" for i in range(20)]
        stored = mc.save_candidates(labels, tmp_path)
        assert stored[:2] == ["a:M", "b:x"] and len(stored) == mc.MAX_COMPARE_CANDIDATES
        assert mc.load_saved_candidates(tmp_path) == stored
        assert (tmp_path / mc.COMPARE_SELECTION_FILE).exists()

    def test_candidate_rows_are_saved_as_labels(self, tmp_path):
        cands = [mc.CompareCandidate("anthropic", "claude", label="Claude"), mc.CompareCandidate("", "bare")]
        assert mc.save_candidates(cands, tmp_path) == ["anthropic:claude", "bare"]

    def test_empty_selection_keeps_the_previous_one(self, tmp_path):
        mc.save_candidates(["a:b"], tmp_path)
        assert mc.save_candidates([], tmp_path) == []
        assert mc.load_saved_candidates(tmp_path) == ["a:b"]

    def test_corrupt_or_wrong_shape_file_reads_as_empty(self, tmp_path):
        path = tmp_path / mc.COMPARE_SELECTION_FILE
        path.write_text("{not json", encoding="utf-8")
        assert mc.load_saved_candidates(tmp_path) == []
        path.write_text('{"models": "a:b"}', encoding="utf-8")
        assert mc.load_saved_candidates(tmp_path) == []
        path.write_text('{"models": ["a:b", 3, null]}', encoding="utf-8")
        assert mc.load_saved_candidates(tmp_path) == ["a:b", "3"]

    def test_default_home_is_the_hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        mc.save_candidates(["a:b"])
        assert mc.compare_selection_path() == tmp_path / mc.COMPARE_SELECTION_FILE
        assert mc.load_saved_candidates() == ["a:b"]


class TestSuggestions:
    def test_defaults_cap_per_provider_and_total(self):
        from agent.model_compare import SUGGEST_MAX_LABELS, suggest_candidate_labels
        providers = [{"slug": "a", "models": [f"a{i}" for i in range(6)]},
                     {"slug": "moa", "models": ["preset"]},
                     {"slug": "b", "models": [f"b{i}" for i in range(6)]},
                     {"slug": "c", "models": [f"c{i}" for i in range(6)]}]
        rows = suggest_candidate_labels(providers, current_provider="a", current_model="a0")
        assert rows[:4] == ["a:a0", "a:a1", "a:a2", "b:b0"] and len(rows) == SUGGEST_MAX_LABELS
        assert "moa:preset" not in rows

    def test_none_lifts_both_caps(self):
        from agent.model_compare import suggest_candidate_labels
        providers = [{"slug": "a", "models": [f"a{i}" for i in range(30)]},
                     {"slug": "b", "models": [f"b{i}" for i in range(30)]}]
        rows = suggest_candidate_labels(providers, current_provider="b", current_model="b7", limit=None, per_provider=None)
        assert len(rows) == 60 and rows[0] == "b:b7" and rows.count("b:b7") == 1


class TestBuildMessages:
    def test_default_system_prompt_and_prompt_last(self):
        msgs = mc.build_messages("q")
        assert msgs[0] == {"role": "system", "content": mc.DEFAULT_SYSTEM_PROMPT}
        assert msgs[-1] == {"role": "user", "content": "q"}

    def test_history_keeps_only_text_turns_and_alternation(self):
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b", "tool_calls": []},
            {"role": "tool", "content": "ignored"},
            {"role": "assistant", "content": [{"type": "text", "text": "not a str"}]},
            {"role": "user", "content": "c"},
        ]
        msgs = mc.build_messages("q", system_prompt="sys", history=history)
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "assistant", "user"]
        # A trailing user turn is folded together with the prompt so alternation holds.
        assert msgs[-1]["content"] == "c\n\nq"
        assert msgs[0]["content"] == "sys"


def _fake_response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message={"content": text})],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})


class TestRunComparison:
    def setup_method(self):
        self.calls = []
        self.lock = threading.Lock()

    def _stub_call_llm(self, delays: dict, failing: set = frozenset()):
        def call_llm(task, *, messages, provider=None, model=None, **kwargs):
            assert task == "model_compare"
            assert messages[0]["role"] == "system"
            with self.lock:
                self.calls.append((provider, model, threading.current_thread().name))
            time.sleep(delays.get(model, 0.0))
            if model in failing:
                raise RuntimeError(f"{model} is down")
            return _fake_response(f"answer from {model}")
        return call_llm

    def test_results_in_candidate_order_and_callback_in_completion_order(self):
        cands = mc.parse_candidate_list(["p:slow", "p:fast"])
        seen = []
        with patch("agent.auxiliary_client.call_llm", self._stub_call_llm({"slow": 0.3, "fast": 0.0})), \
                patch("agent.moa_loop._slot_runtime", lambda slot: dict(slot)):
            results = mc.run_comparison(cands, "hi", on_result=lambda r, done, total: seen.append((r.model, done, total)))
        assert [r.model for r in results] == ["slow", "fast"]
        assert all(r.ok for r in results)
        assert results[0].text == "answer from slow"
        assert results[1].input_tokens == 7 and results[1].output_tokens == 3
        assert seen == [("fast", 1, 2), ("slow", 2, 2)]
        # Parallel: two distinct worker threads served the two candidates.
        assert len({name for _, _, name in self.calls}) == 2

    def test_one_failure_does_not_sink_the_others(self):
        cands = mc.parse_candidate_list(["p:ok", "p:bad"])
        with patch("agent.auxiliary_client.call_llm", self._stub_call_llm({}, failing={"bad"})), \
                patch("agent.moa_loop._slot_runtime", lambda slot: dict(slot)):
            results = mc.run_comparison(cands, "hi")
        assert results[0].ok and results[0].text == "answer from ok"
        assert not results[1].ok and "bad is down" in results[1].error
        assert results[1].to_dict()["ok"] is False

    def test_empty_prompt_rejected_and_no_candidates_is_noop(self):
        with pytest.raises(ValueError):
            mc.run_comparison(mc.parse_candidate_list(["p:m"]), "   ")
        assert mc.run_comparison([], "hi") == []

    def test_empty_answer_is_an_error(self):
        cands = mc.parse_candidate_list(["p:m"])
        with patch("agent.auxiliary_client.call_llm", lambda *a, **k: _fake_response("")), \
                patch("agent.moa_loop._slot_runtime", lambda slot: dict(slot)):
            (result,) = mc.run_comparison(cands, "hi")
        assert result.error == "empty response"


class TestFormatting:
    def test_result_message_has_label_body_and_stats(self):
        r = mc.CompareResult("p", "m", "p:m", text="Body", elapsed_s=2.5, input_tokens=10, output_tokens=4, cost_usd=0.5)
        text = mc.format_result_message(r, index=1, total=3)
        assert text.startswith("🔬 **[1/3] p:m**")
        assert "Body" in text and "2.5s" in text and "10→4 tokens" in text and "$0.50" in text

    def test_result_message_truncates_and_reports_failures(self):
        r = mc.CompareResult("p", "m", "p:m", text="x" * 50, elapsed_s=1)
        assert "truncated" in mc.format_result_message(r, index=1, total=1, max_chars=10)
        failed = mc.CompareResult("p", "m", "p:m", error="boom", elapsed_s=1)
        assert "❌ Failed: boom" in mc.format_result_message(failed, index=2, total=2)

    def test_summary_orders_ok_fastest_first(self):
        results = [
            mc.CompareResult("p", "slow", "slow", text="a", elapsed_s=5),
            mc.CompareResult("p", "bad", "bad", error="nope", elapsed_s=0.1),
            mc.CompareResult("p", "fast", "fast", text="b", elapsed_s=1),
        ]
        lines = mc.format_summary_message(results).splitlines()
        assert lines[1].startswith("✅ fast") and lines[2].startswith("✅ slow") and lines[3].startswith("❌ bad")
        assert mc.format_summary_message([]).startswith("📊")

    def test_format_cost(self):
        assert mc.format_cost(None) == "n/a"
        assert mc.format_cost(0) == "$0"
        assert mc.format_cost(0.0004) == "$0.0004"
        assert mc.format_cost(1.5) == "$1.50"
