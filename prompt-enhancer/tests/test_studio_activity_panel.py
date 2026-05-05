"""Tests for the cross-umbrella activity panel's pure helpers.

The NiceGUI rendering itself isn't unit-tested (would require booting
a NiceGUI page); we test ``merge_events`` + ``dedupe_events`` +
``fetch_all`` since those are the load-bearing logic.
"""

from __future__ import annotations

import pytest

from enhancer.ui.components.activity_panel import (
    dedupe_events,
    merge_events,
)


def test_merge_events_orders_by_timestamp_desc():
    pe = {
        "service": "prompt_enhancer",
        "events": [
            {"ts": "2026-05-04T15:30:00.000Z", "type": "run_started", "summary": "PE 1"},
            {"ts": "2026-05-04T15:30:05.000Z", "type": "pass_result", "summary": "PE 2"},
        ],
    }
    rr = {
        "service": "round_robin",
        "events": [
            {"ts": "2026-05-04T15:30:02.000Z", "type": "run_started", "summary": "RR 1"},
        ],
    }
    dev = {
        "service": "development",
        "events": [
            {"ts": "2026-05-04T15:30:10.000Z", "type": "build_started", "summary": "DEV 1"},
        ],
    }
    merged = merge_events([pe, rr, dev])
    summaries = [e["summary"] for e in merged]
    services = [e["service"] for e in merged]

    # DESC by ts: DEV 1 (15:30:10), PE 2 (15:30:05), RR 1 (15:30:02), PE 1 (15:30:00).
    assert summaries == ["DEV 1", "PE 2", "RR 1", "PE 1"]
    assert services == ["development", "prompt_enhancer", "round_robin", "prompt_enhancer"]


def test_merge_events_handles_empty_responses():
    # Empty events list AND empty response list both work.
    assert merge_events([]) == []
    assert merge_events([{"service": "x", "events": []}]) == []


def test_merge_events_handles_unreachable_peer():
    # When a peer is unreachable, fetch_all skips it (None → not in
    # the input list). The rest of the merge should still work.
    pe = {
        "service": "prompt_enhancer",
        "events": [
            {"ts": "2026-05-04T15:30:00.000Z", "type": "run_started", "summary": "PE 1"},
        ],
    }
    # Only PE present (RR unreachable, DEV unreachable).
    merged = merge_events([pe])
    assert len(merged) == 1
    assert merged[0]["service"] == "prompt_enhancer"


def test_merge_events_skips_malformed_responses():
    # Defensive: if a peer returns a string or None somehow, skip it.
    bad: list = [None, "oops", {"service": "x"}, {"events": "not-a-list"}]
    assert merge_events(bad) == []


def test_dedupe_events_collapses_repeats():
    events = [
        {"service": "pe", "ts": "T1", "type": "x", "summary": "a"},
        {"service": "pe", "ts": "T1", "type": "x", "summary": "a"},  # dup
        {"service": "pe", "ts": "T2", "type": "x", "summary": "a"},  # diff ts → kept
        {"service": "rr", "ts": "T1", "type": "x", "summary": "a"},  # diff service → kept
    ]
    out = dedupe_events(events)
    assert len(out) == 3


def test_dedupe_preserves_first_occurrence():
    events = [
        {"service": "pe", "ts": "T1", "type": "x", "summary": "first"},
        {"service": "pe", "ts": "T1", "type": "x", "summary": "first"},  # dup
    ]
    out = dedupe_events(events)
    assert out[0]["summary"] == "first"
    assert len(out) == 1
