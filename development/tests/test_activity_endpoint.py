"""Tests for development's ``GET /api/activity`` endpoint.

development reuses its existing MessageBoard rather than maintaining a
parallel ring buffer — the activity translator reads board.recent() and
maps each StageEvent.kind to the umbrella wire format. See
``development/activity.py`` for the rationale.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.server import create_app
from development.stages import ArchitectStage
from development.types import (
    BUILD_DONE,
    BUILD_FAILED,
    BUILD_STARTED,
    STAGE_DONE,
    STAGE_FAILED,
    STAGE_STARTED,
)

from tests.conftest import FakeLMClient


@pytest.fixture
def client(fake_lm: FakeLMClient, tmp_board: MessageBoard) -> TestClient:
    orch = Orchestrator(
        fake_lm, tmp_board, stages=[ArchitectStage(fake_lm)],
    )
    app = create_app(message_board=tmp_board, orchestrator=orch)
    with TestClient(app) as c:
        yield c


def test_activity_empty_returns_200(client: TestClient):
    r = client.get("/api/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "development"
    assert body["events"] == []


def test_activity_records_build_lifecycle(client: TestClient):
    # Trigger a build → orchestrator publishes BUILD_STARTED, STAGE_STARTED,
    # STAGE_DONE, BUILD_DONE to the message board. /api/activity translates.
    r = client.post("/api/build", json={"goal": "tiny notes app"})
    assert r.status_code == 200

    body = client.get("/api/activity").json()
    events = body["events"]
    assert len(events) >= 3  # build_started, at least one stage, build_done
    types = [e["type"] for e in events]
    assert "build_started" in types
    assert "stage" in types
    assert "build_done" in types

    # Wire-shape contract.
    for ev in events:
        assert "ts" in ev and ev["ts"].endswith("Z")
        assert "type" in ev
        assert "summary" in ev
        assert len(ev["summary"]) <= 120


def test_activity_orders_newest_first(
    client: TestClient, tmp_board: MessageBoard
):
    # Push events in known order, verify we read newest-first.
    tmp_board.publish(BUILD_STARTED, {"request": {"goal": "old", "stack_hint": "py"}})
    tmp_board.publish(STAGE_STARTED, {"stage": "architect"})
    tmp_board.publish(STAGE_DONE, {"stage": "architect"})
    tmp_board.publish(BUILD_DONE, {"result": {}})

    body = client.get("/api/activity").json()
    types = [e["type"] for e in body["events"]]
    # build_done first (newest), build_started last (oldest).
    assert types[0] == "build_done"
    assert types[-1] == "build_started"


def test_activity_translates_build_failed(
    client: TestClient, tmp_board: MessageBoard
):
    tmp_board.publish(BUILD_STARTED, {"request": {"goal": "x"}})
    tmp_board.publish(STAGE_FAILED, {"stage": "coder", "error": "syntax"})
    tmp_board.publish(BUILD_FAILED, {"stage": "coder", "error": "syntax"})

    body = client.get("/api/activity").json()
    error_events = [e for e in body["events"] if e["type"] == "error"]
    assert len(error_events) >= 1
    # Build-failed shows up as an error.
    summaries = " ".join(e["summary"] for e in error_events)
    assert "failed" in summaries.lower()


def test_activity_limit_clamped(client: TestClient, tmp_board: MessageBoard):
    # Push 60 events, verify the default limit of 50.
    for _ in range(60):
        tmp_board.publish(BUILD_STARTED, {"request": {"goal": "x"}})

    body = client.get("/api/activity").json()
    assert len(body["events"]) == 50

    body = client.get("/api/activity?limit=1").json()
    assert len(body["events"]) == 1

    body = client.get("/api/activity?limit=500").json()
    # silently clamped to 200 max
    assert len(body["events"]) <= 200


def test_activity_filters_progress_noise(
    client: TestClient, tmp_board: MessageBoard
):
    # STAGE_PROGRESS is dropped from /api/activity (too noisy for the
    # umbrella feed). The SSE /api/events still streams it.
    from development.types import STAGE_PROGRESS

    tmp_board.publish(BUILD_STARTED, {"request": {"goal": "x"}})
    tmp_board.publish(STAGE_PROGRESS, {"stage": "coder", "progress": "50%"})
    tmp_board.publish(BUILD_DONE, {"result": {}})

    body = client.get("/api/activity").json()
    types = [e["type"] for e in body["events"]]
    assert "build_started" in types
    assert "build_done" in types
    # No translated event for STAGE_PROGRESS.
    assert all(t != "stage_progress" for t in types)
