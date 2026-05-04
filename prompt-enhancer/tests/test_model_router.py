"""Tests for `enhancer.llm.model_router`.

Pure-function tests; no fixtures, no monkeypatching.
"""

from __future__ import annotations

from enhancer.llm.model_router import select_default, select_scorer


# ── preferred override ──────────────────────────────────────────────


def test_preferred_wins_when_available():
    models = ["llama-3-8b", "qwen3-coder-30b", "hermes-3"]
    assert (
        select_scorer("creative", models, preferred="llama-3-8b") == "llama-3-8b"
    )


def test_preferred_ignored_when_not_in_list():
    models = ["llama-3-8b", "qwen3-coder-30b"]
    # "hermes-3" not in models → fall through to creative rules; first
    # rule "hermes" misses, "mistral" misses, "llama-3" hits.
    assert (
        select_scorer("creative", models, preferred="hermes-3") == "llama-3-8b"
    )


def test_preferred_overrides_task_rule():
    """Preferred wins even if it's a "worse" choice than the rule pick."""
    models = ["qwen3-coder-30b", "llama-3-8b"]
    # analytical rule would prefer qwen3-coder; preferred forces llama.
    assert (
        select_scorer("analytical", models, preferred="llama-3-8b") == "llama-3-8b"
    )


# ── per-task routing rules ──────────────────────────────────────────


def test_analytical_prefers_qwen3_coder():
    models = ["llama-3-8b", "deepseek-coder-6.7b", "qwen3-coder-30b"]
    assert select_scorer("analytical", models) == "qwen3-coder-30b"


def test_analytical_falls_back_to_deepseek_coder():
    models = ["llama-3-8b", "deepseek-coder-6.7b", "mistral-7b"]
    assert select_scorer("analytical", models) == "deepseek-coder-6.7b"


def test_analytical_falls_back_to_llama_when_no_coder():
    models = ["llama-3-8b", "mistral-7b", "hermes-3"]
    assert select_scorer("analytical", models) == "llama-3-8b"


def test_research_prefers_qwen3():
    models = ["llama-3-8b", "qwen3-30b", "deepseek-r1"]
    assert select_scorer("research", models) == "qwen3-30b"


def test_research_falls_through_to_deepseek():
    models = ["llama-3-8b", "deepseek-r1-distill", "mistral-7b"]
    assert select_scorer("research", models) == "deepseek-r1-distill"


def test_creative_prefers_hermes():
    models = ["llama-3-8b", "hermes-3-70b", "qwen3-30b"]
    assert select_scorer("creative", models) == "hermes-3-70b"


def test_creative_falls_through_to_mistral():
    models = ["llama-3-8b", "mistral-nemo", "qwen3-30b"]
    assert select_scorer("creative", models) == "mistral-nemo"


def test_instructional_prefers_qwen3_coder():
    models = ["llama-3-8b", "qwen3-coder-30b", "qwen2.5-7b"]
    assert select_scorer("instructional", models) == "qwen3-coder-30b"


def test_instructional_falls_through_to_qwen():
    models = ["llama-3-8b", "qwen2.5-7b"]
    # "qwen3-coder" misses (no qwen3-coder), "qwen" matches qwen2.5.
    assert select_scorer("instructional", models) == "qwen2.5-7b"


def test_coding_uses_same_rules_as_analytical():
    """`coding` is the post-processed override of instructional+code, but
    routes the same way as analytical (both want a coder model)."""
    models = ["llama-3-8b", "qwen3-coder-30b", "hermes-3"]
    assert select_scorer("coding", models) == "qwen3-coder-30b"


def test_factual_routes_to_qwen3():
    models = ["llama-3-8b", "qwen3-30b", "hermes-3"]
    assert select_scorer("factual", models) == "qwen3-30b"


# ── no-rule / unknown task type fallthrough ─────────────────────────


def test_conversational_has_no_rules_falls_back_to_alpha():
    """`conversational` is a canonical task type but has no router rule
    — should hit the alphabetic-first fallback."""
    models = ["zebra-llm", "alpha-llm", "middle-llm"]
    assert select_scorer("conversational", models) == "alpha-llm"


def test_unknown_task_type_falls_back_to_alpha():
    models = ["zebra-llm", "alpha-llm"]
    assert select_scorer("nonsense-type", models) == "alpha-llm"


def test_empty_task_type_falls_back_to_alpha():
    models = ["zebra-llm", "alpha-llm"]
    assert select_scorer("", models) == "alpha-llm"


def test_no_rule_match_in_task_type_falls_back_to_alpha():
    """task_type has rules, but no available model matches any of them."""
    models = ["bert-base", "albert-large", "roberta"]
    # creative rules (hermes, mistral, llama-3, qwen) all miss.
    assert select_scorer("creative", models) == "albert-large"


# ── empty / edge cases ──────────────────────────────────────────────


def test_empty_list_returns_empty_string():
    assert select_scorer("analytical", []) == ""
    assert select_scorer("creative", [], preferred="hermes-3") == ""
    assert select_scorer("", []) == ""


def test_single_model_short_circuits():
    assert select_scorer("analytical", ["only-model"]) == "only-model"


# ── case insensitivity on model ids ─────────────────────────────────


def test_mixed_case_model_id_matches_lowercased_substring():
    models = ["Qwen3-Coder-30B-Instruct", "Llama-3-8B"]
    assert (
        select_scorer("analytical", models) == "Qwen3-Coder-30B-Instruct"
    )


def test_uppercase_model_id_matches_creative_rule():
    models = ["LLAMA-3-8B", "HERMES-3-70B-Instruct"]
    # rules are lowercased; substring "hermes" matches "HERMES-…".
    assert select_scorer("creative", models) == "HERMES-3-70B-Instruct"


def test_mixed_case_task_type_normalizes():
    """task_type comes from a lowercased canonical set, but be defensive."""
    models = ["qwen3-coder-30b", "llama-3-8b"]
    assert select_scorer("ANALYTICAL", models) == "qwen3-coder-30b"


# ── select_default ──────────────────────────────────────────────────


def test_select_default_prefers_preferred():
    models = ["alpha", "beta"]
    assert select_default(models, preferred="beta") == "beta"


def test_select_default_falls_back_to_alpha():
    models = ["zebra", "alpha"]
    assert select_default(models) == "alpha"


def test_select_default_preferred_not_in_list_falls_back():
    models = ["zebra", "alpha"]
    assert select_default(models, preferred="missing") == "alpha"


def test_select_default_empty_list():
    assert select_default([]) == ""
    assert select_default([], preferred="anything") == ""


# ── caller-order preserved within one substring rule ────────────────


def test_priority_within_substring_uses_caller_order():
    """Two models match the same substring; first one in caller order wins.
    This matters because callers pass loaded-first sorted lists from
    lms_discovery — we want the loaded model picked over the unloaded.
    """
    # Both contain "qwen3-coder"; first in list (loaded) should win.
    models = ["qwen3-coder-loaded", "qwen3-coder-unloaded"]
    assert select_scorer("analytical", models) == "qwen3-coder-loaded"
