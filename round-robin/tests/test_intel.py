from round_robin.intel import (
    COLLAB_DIRECTIVE,
    DialogueAnalyzer,
    IntelConfig,
    Nudge,
    agreement_signals,
    closure_signals,
    redundancy_score,
)


def test_collab_directive_is_non_trivial():
    assert "critical collaborator" in COLLAB_DIRECTIVE
    assert len(COLLAB_DIRECTIVE) > 100


def test_closure_signals_detected():
    samples = [
        "In summary, this is great.",
        "Let me know if you need anything else!",
        "I think we're done here.",
        "Sounds great, ready to implement.",
        "That about wraps it up.",
        "Hope that helps.",
        "Final thoughts: this works.",
    ]
    for s in samples:
        assert closure_signals(s), f"missed closure in: {s!r}"


def test_closure_no_false_positive():
    text = "Let's dig deeper into the database schema and explore alternatives."
    assert closure_signals(text) == []


def test_agreement_signals_detected():
    samples = [
        "I agree with that approach.",
        "Great point — exactly what I was thinking.",
        "Spot on, makes sense.",
        "+1 on this design.",
        "Couldn't agree more.",
        "You're absolutely right.",
    ]
    for s in samples:
        assert agreement_signals(s), f"missed agreement in: {s!r}"


def test_agreement_no_false_positive():
    text = "I'd push back: the database choice has measurable trade-offs."
    assert agreement_signals(text) == []


def test_redundancy_score_identical():
    text = "We should use Postgres for transactional data and Redis for caching."
    assert redundancy_score(text, text) >= 0.99


def test_redundancy_score_unrelated():
    a = "We should use Postgres for transactional data."
    b = "Consider sharding strategies based on tenant ID."
    assert redundancy_score(a, b) < 0.3


def test_redundancy_score_empty():
    assert redundancy_score("", "anything") == 0.0
    assert redundancy_score("anything", "") == 0.0


def test_maybe_nudge_returns_closure_nudge():
    transcript = [
        {"agent": "orchestrator", "content": "theme: x"},
        {"agent": "Alpha", "content": "First take on the design."},
        {"agent": "Bravo", "content": "In summary, the design is solid. Let me know if you need anything else."},
    ]
    n = DialogueAnalyzer.maybe_nudge(transcript, "Bravo", turns_remaining=10, cfg=IntelConfig())
    assert n is not None
    assert n.reason == "closure"
    assert "remaining" in n.content


def test_maybe_nudge_returns_none_on_last_turn():
    transcript = [
        {"agent": "Alpha", "content": "In summary, we're done."},
    ]
    assert DialogueAnalyzer.maybe_nudge(transcript, "Alpha", turns_remaining=0, cfg=IntelConfig()) is None


def test_maybe_nudge_redundant():
    prev = "We should benchmark Postgres vs MySQL on a representative workload."
    transcript = [
        {"agent": "orchestrator", "content": "theme: x"},
        {"agent": "Alpha", "content": prev},
        {"agent": "Bravo", "content": "Mid response with new content here."},
        {"agent": "Alpha", "content": prev + " Also again."},   # near-identical
    ]
    n = DialogueAnalyzer.maybe_nudge(transcript, "Alpha", turns_remaining=5,
                                     cfg=IntelConfig(redundancy_threshold=0.6))
    assert n is not None
    assert n.reason == "redundant"


def test_maybe_nudge_brief():
    transcript = [
        {"agent": "orchestrator", "content": "theme"},
        {"agent": "Alpha", "content": "long enough turn one with several distinct words here please"},
        {"agent": "Bravo", "content": "another sufficiently long turn two so we get past warmup"},
        {"agent": "Alpha", "content": "ok"},   # very brief
    ]
    n = DialogueAnalyzer.maybe_nudge(transcript, "Alpha", turns_remaining=5,
                                     cfg=IntelConfig(brief_threshold_tokens=10))
    assert n is not None
    assert n.reason == "brief"


def test_maybe_nudge_disabled():
    transcript = [{"agent": "Alpha", "content": "In summary, we're done."}]
    cfg = IntelConfig(anti_rambling=False, anti_yes_man=False)
    assert DialogueAnalyzer.maybe_nudge(transcript, "Alpha", turns_remaining=5, cfg=cfg) is None


def test_contrarian_nudge_format():
    n = DialogueAnalyzer.contrarian_nudge(streak=3)
    assert isinstance(n, Nudge)
    assert n.reason == "agreement_streak"
    assert "3" in n.content
    assert "opposite" in n.content


def test_has_agreement_helper():
    assert DialogueAnalyzer.has_agreement("I agree completely.") is True
    assert DialogueAnalyzer.has_agreement("That's a separate concern.") is False
