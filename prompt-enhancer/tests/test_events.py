"""EventType enum regression guards.

The enum at ``enhancer/core/events.py`` is the standalone's public API
boundary. The swarm-agent-dev monolith, the analytics dashboard, and any
JSONL-stream consumer reads against these names.

These tests are paranoid by design:

1. ``test_all_36_event_names_present`` — the v2.0 catalog is complete.
2. ``test_v2_additions_have_nonempty_values`` — the 6 new members carry
   non-empty ``.value`` strings (so JSON-serialization keeps working).
3. ``test_v1_names_still_resolve`` — the 30 v1.x names are still present
   under the same enum spelling (regression guard against accidental
   rename — v1 stream consumers must keep working in v2.x).

If any of these fail, somebody touched the frozen enum. Bump
``__version__`` to v3 and ship a compat layer per the protocol in
``docs/EVENTS.md`` § "Removing or renaming" instead.
"""

from __future__ import annotations

from enhancer.core.events import EventType


# ── canonical reference sets ──────────────────────────────────────────

V1_NAMES: frozenset[str] = frozenset({
    # pipeline backbone (8)
    "AGENT_STEP",
    "AGENT_PASS_START",
    "AGENT_PASS_CHUNK",
    "AGENT_PASS_RESULT",
    "AGENT_PIPELINE_SUMMARY",
    "ENHANCEMENT_SCORE",
    "AGENT_DONE",
    "AGENT_ERROR",
    # interactive disambiguation (1)
    "AGENT_DISAMBIGUATE",
    # persona (2)
    "PERSONA_START",
    "PERSONA_RESULT",
    # magnitude (4)
    "MAGNITUDE_START",
    "MAGNITUDE_CHUNK",
    "MAGNITUDE_DONE",
    "MAGNITUDE_ERROR",
    # skeleton of thought (4)
    "SOT_START",
    "SOT_CHUNK",
    "SOT_DONE",
    "SOT_ERROR",
    # pretrial (3)
    "PRETRIAL_START",
    "PRETRIAL_RESULT",
    "PRETRIAL_ERROR",
    # sessions (8)
    "SESSION_CREATED",
    "SESSION_LIST",
    "SESSION_LOADED",
    "SESSION_RENAMED",
    "SESSION_CLEARED",
    "SESSION_DELETED",
    "SESSION_ENTRY_ADDED",
    "SESSION_ACTIVE",
})

V2_ADDITIONS: frozenset[str] = frozenset({
    # provider health (2)
    "PROVIDER_HEALTH_OPEN",
    "PROVIDER_HEALTH_CLOSED",
    # MCP tool invocation (2)
    "MCP_TOOL_INVOKED",
    "MCP_TOOL_RESULT",
    # branching (2)
    "BRANCHING_FORK",
    "BRANCHING_MERGE",
})

# Members added after v2.0 in additive patches. The enum-frozen rule
# applies to v1 names only; v2.x is allowed to grow.
V2_PATCH_ADDITIONS: frozenset[str] = frozenset({
    # persona partner (1) — round-robin Bravo (v2.0.x patch)
    "PERSONA_PARTNER_RESULT",
})

EXPECTED_NAMES: frozenset[str] = V1_NAMES | V2_ADDITIONS | V2_PATCH_ADDITIONS


# ── tests ─────────────────────────────────────────────────────────────

def test_v1_v2_sets_disjoint() -> None:
    """Sanity check on the test fixtures themselves."""
    assert V1_NAMES.isdisjoint(V2_ADDITIONS)
    assert V1_NAMES.isdisjoint(V2_PATCH_ADDITIONS)
    assert V2_ADDITIONS.isdisjoint(V2_PATCH_ADDITIONS)
    assert len(V1_NAMES) == 30
    assert len(V2_ADDITIONS) == 6
    assert len(V2_PATCH_ADDITIONS) == 1
    assert len(EXPECTED_NAMES) == 37


def test_all_36_event_names_present() -> None:
    """Every expected name must resolve to an EventType member."""
    actual = {member.name for member in EventType}
    missing = EXPECTED_NAMES - actual
    extra = actual - EXPECTED_NAMES

    assert not missing, (
        f"EventType is missing {len(missing)} expected member(s): "
        f"{sorted(missing)}. v1 names are FROZEN; new members must be "
        "added (not renamed). See docs/EVENTS.md."
    )
    assert not extra, (
        f"EventType has {len(extra)} unexpected member(s): "
        f"{sorted(extra)}. Update tests/test_events.py and "
        "docs/MIGRATION.md if this addition is intentional."
    )
    assert len(actual) == 37, f"expected 37 members, got {len(actual)}"


def test_v2_additions_have_nonempty_values() -> None:
    """The 6 v2 members must all carry non-empty ``.value`` strings.

    ``EventType`` is a ``str``-mixin enum; an empty value would silently
    serialize to ``""`` in JSONL streams and break analytics consumers.
    """
    for name in sorted(V2_ADDITIONS):
        member = EventType[name]
        assert isinstance(member.value, str), (
            f"{name}.value is not a str (got {type(member.value).__name__})"
        )
        assert member.value, f"{name} has an empty .value"
        # Values are conventionally lowercase_snake_case.
        assert member.value == member.value.lower(), (
            f"{name}.value should be lowercase, got {member.value!r}"
        )


def test_v1_names_still_resolve() -> None:
    """All 30 v1 names must still resolve via ``EventType[name]``.

    Regression guard against accidental rename. v2.x must continue to
    emit every v1 event name unchanged; see ``docs/MIGRATION.md`` §
    "Compatibility commitment".
    """
    for name in sorted(V1_NAMES):
        member = EventType[name]  # raises KeyError if renamed/removed
        assert isinstance(member.value, str)
        assert member.value, f"{name} resolved but has empty .value"


def test_v2_values_are_unique_and_distinct_from_v1() -> None:
    """No v2 value collides with another enum value."""
    values = [m.value for m in EventType]
    assert len(values) == len(set(values)), (
        f"EventType has duplicate .value entries: {sorted(values)}"
    )
    v1_values = {EventType[n].value for n in V1_NAMES}
    v2_values = {EventType[n].value for n in V2_ADDITIONS}
    assert v1_values.isdisjoint(v2_values)
