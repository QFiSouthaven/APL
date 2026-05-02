"""Unit tests for ``enhancer.core.parsing`` — the LLM-output normalizers.

These guard against the noisy-LLM-output trap: parsers must extract
canonical values from messy text, fall back to safe defaults, and
preserve the "instructional + code → coding" override.
"""

from __future__ import annotations

import pytest

from enhancer.core.parsing import (
    clamp,
    coerce_task_type_for_code,
    count_weakness_fields,
    parse_disambiguate_questions,
    parse_persona,
    parse_scores,
    parse_task_type,
    parse_technique,
)


# ── clamp ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "val,lo,hi,default,expected",
    [
        (0.5, 0.0, 1.0, 0.7, 0.5),     # in range
        (-1.0, 0.0, 1.0, 0.7, 0.0),    # below
        (5.0, 0.0, 1.0, 0.7, 1.0),     # above
        (None, 0.0, 1.0, 0.7, 0.7),    # None → default
        ("nope", 0.0, 1.0, 0.7, 0.7),  # non-numeric → default
        ("0.42", 0.0, 1.0, 0.7, 0.42), # numeric string
    ],
)
def test_clamp(val, lo, hi, default, expected):
    assert clamp(val, lo, hi, default) == expected


# ── parse_task_type ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("TASK TYPE: creative", "creative"),
        ("task type: analytical", "analytical"),
        # Noise keyword extraction
        ("TASK TYPE: instructional (creating a feature)", "instructional"),
        ("TASK TYPE: analytical|instructional", "analytical"),
        # No keyword present → raw fallthrough
        ("TASK TYPE: research", "research"),
        ("", ""),
    ],
)
def test_parse_task_type(text, expected):
    assert parse_task_type(text) == expected


def test_coerce_task_type_for_code_overrides_instructional():
    assert coerce_task_type_for_code("instructional", "Implement an API") == "coding"
    assert coerce_task_type_for_code("instructional", "Write an essay") == "instructional"
    # Non-instructional types are never overridden.
    assert coerce_task_type_for_code("creative", "Implement a function") == "creative"


# ── parse_technique ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("PRIMARY FOCUS: precision", "precision"),
        ("PRIMARY FOCUS: CONTEXT", "context"),
        ("PRIMARY FOCUS: structure", "structure"),
        # Invalid → default precision
        ("PRIMARY FOCUS: clarity", "precision"),
        ("", "precision"),
    ],
)
def test_parse_technique(text, expected):
    assert parse_technique(text) == expected


# ── parse_persona ──────────────────────────────────────────────────

def test_parse_persona_returns_raw_value():
    text = "PERSONA: Senior Distributed Systems Architect"
    assert parse_persona(text) == "Senior Distributed Systems Architect"


def test_parse_persona_empty_on_miss():
    assert parse_persona("no persona here") == ""


# ── parse_scores ───────────────────────────────────────────────────

def test_parse_scores_full():
    text = (
        "SPECIFICITY: 9\n"
        "CONSTRAINTS: 8\n"
        "ACTIONABILITY: 7\n"
        "IMPROVEMENT: 65\n"
    )
    assert parse_scores(text) == {
        "specificity": 9, "constraints": 8,
        "actionability": 7, "improvement": 65,
    }


def test_parse_scores_defaults_on_garbage():
    """Missing or unparseable lines fall back to defaults."""
    text = "SPECIFICITY: nine\nIMPROVEMENT: not-a-number\n"
    out = parse_scores(text)
    assert out["specificity"] == 5      # default
    assert out["constraints"] == 5      # missing
    assert out["actionability"] == 5    # missing
    assert out["improvement"] == 50     # default


def test_parse_scores_handles_extra_text_after_number():
    text = "SPECIFICITY: 8 points\nCONSTRAINTS: 7\nACTIONABILITY: 9\nIMPROVEMENT: 60\n"
    assert parse_scores(text)["specificity"] == 8


# ── count_weakness_fields ──────────────────────────────────────────

def test_count_weakness_fields_skips_none_and_na():
    text = (
        "VAGUE TERMS: none\n"
        "MISSING CONTEXT: audience details\n"
        "UNSTATED CONSTRAINTS: format unspecified\n"
        "SCOPE ISSUES: n/a\n"
    )
    assert count_weakness_fields(text) == 2


def test_count_weakness_fields_all_populated_triggers_disambig():
    text = (
        "VAGUE TERMS: ambiguous wording\n"
        "MISSING CONTEXT: audience\n"
        "UNSTATED CONSTRAINTS: length\n"
        "SCOPE ISSUES: too broad\n"
    )
    assert count_weakness_fields(text) >= 3


# ── parse_disambiguate_questions ──────────────────────────────────

def test_parse_disambiguate_questions_basic():
    text = (
        "Q1: What audience is this for?\n"
        "A) developers\nB) end users\nC) executives\n\n"
        "Q2: What output format?\n"
        "A) markdown\nB) plain text\nC) JSON\n"
    )
    qs = parse_disambiguate_questions(text)
    assert len(qs) == 2
    assert qs[0]["options"] == ["developers", "end users", "executives"]
    assert qs[1]["question"] == "What output format?"


def test_parse_disambiguate_questions_skips_double_digit_options():
    """`10)` is intentionally NOT recognized — design choice; max 2-3 opts."""
    text = "Q1: pick one\n10) too many\nA) one\nB) two\n"
    qs = parse_disambiguate_questions(text)
    assert qs[0]["options"] == ["one", "two"]
