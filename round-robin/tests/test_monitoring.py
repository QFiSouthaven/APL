import json
from pathlib import Path

from round_robin.monitoring import ErrorMonitor


def test_record_appends_to_log_and_ring(tmp_path: Path) -> None:
    mon = ErrorMonitor(log_path=tmp_path / "errors.log", ring_size=5)
    e = mon.record("agent", "Bravo timed out", turn=2, agent_name="Bravo")
    assert e.id.startswith("err-")
    assert e.category == "agent"
    assert e.context["turn"] == 2

    log_lines = (tmp_path / "errors.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(log_lines) == 1
    parsed = json.loads(log_lines[0])
    assert parsed["message"] == "Bravo timed out"
    assert parsed["context"]["agent_name"] == "Bravo"


def test_ring_eviction(tmp_path: Path) -> None:
    mon = ErrorMonitor(log_path=tmp_path / "errors.log", ring_size=3)
    for i in range(7):
        mon.record("system", f"err-{i}")
    items = mon.recent(limit=10)
    assert len(items) == 3
    # newest first
    messages = [e.message for e in items]
    assert messages == ["err-6", "err-5", "err-4"]


def test_filter_by_category(tmp_path: Path) -> None:
    mon = ErrorMonitor(log_path=tmp_path / "errors.log")
    mon.record("agent", "a")
    mon.record("charlie", "b")
    mon.record("agent", "c")
    only_agent = mon.recent(category="agent")
    assert {e.message for e in only_agent} == {"a", "c"}


def test_clear_resets_ring_only(tmp_path: Path) -> None:
    log = tmp_path / "errors.log"
    mon = ErrorMonitor(log_path=log)
    mon.record("system", "boom")
    n = mon.clear()
    assert n == 1
    assert mon.recent() == []
    # Disk log preserved
    assert log.exists() and "boom" in log.read_text(encoding="utf-8")


def test_stats(tmp_path: Path) -> None:
    mon = ErrorMonitor(log_path=tmp_path / "errors.log")
    mon.record("agent", "x")
    mon.record("agent", "y")
    mon.record("charlie", "z")
    stats = mon.stats()
    assert stats["total"] == 3
    assert stats["by_category"] == {"agent": 2, "charlie": 1}


def test_context_sanitization(tmp_path: Path) -> None:
    """Non-JSON-serializable values get coerced; long strings truncated."""
    mon = ErrorMonitor(log_path=tmp_path / "errors.log")
    class Weird:
        def __repr__(self): return "<Weird>"
    e = mon.record("system", "msg", obj=Weird(), big="x" * 1000)
    assert e.context["obj"] == "<Weird>"
    assert len(e.context["big"]) == 500
    # Round-trips as JSON
    assert json.dumps(e.context)


def test_record_truncates_long_message(tmp_path: Path) -> None:
    mon = ErrorMonitor(log_path=tmp_path / "errors.log")
    e = mon.record("system", "x" * 5000)
    assert len(e.message) == 2000
