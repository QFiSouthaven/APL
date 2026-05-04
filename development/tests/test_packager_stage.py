"""Stage-level tests for the Packager (v0.5).

The Packager's responsibilities:

  1. Read ``ctx["plan"]`` + ``ctx["artifacts_by_layer"]``; ask the LLM
     for a JSON map of packaging file paths → contents.
  2. Mount each emitted file under BOTH ``ctx["artifacts"]`` (flat) AND
     ``ctx["artifacts_by_layer"]["packaging"]`` (nested).
  3. Validate each known-shape file structurally via
     :mod:`development.stages._packager_validator`.
  4. Record per-file verdicts in ``ctx["package_validation"]``;
     surface them on ``BuildResult.package_validation``.
  5. NEVER fail the build — Packager is informational, not a gate.

Almost every test below uses :class:`FakeLMClient` with hand-crafted
fake responses that exercise specific validator paths. The validator
tests live in this file too (rather than a separate module) so the
relationship between the prompt and the validator is visible at a
glance.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from development.messageboard import MessageBoard
from development.orchestrator import Orchestrator
from development.stages import (
    ArchitectStage,
    CoderStage,
    PackagerStage,
    ReviewerStage,
    TesterStage,
)
from development.stages import _packager_validator as validator_module
from development.stages.packager import (
    REQUIRED_FILES,
    RETRY_REMINDER,
    SYSTEM_PROMPT,
)
from development.types import STAGE_PROGRESS, BuildRequest

from tests.conftest import FakeLMClient


# ── helpers ─────────────────────────────────────────────────────────


# A clean six-file packaging set the LLM "would" produce for a happy
# FastAPI + SQLite plan. Used by every happy-path test as a baseline;
# individual tests override one file at a time to exercise a specific
# validator path.
GOOD_DOCKERFILE = """\
# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY . .
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
"""

GOOD_COMPOSE_FASTAPI_SQLITE = """\
services:
  backend:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=sqlite:///./app.db
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
"""

GOOD_COMPOSE_POSTGRES = """\
services:
  backend:
    build: .
    ports:
      - "8000:8000"
    depends_on:
      - db
  db:
    image: postgres:16-alpine
    volumes:
      - db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
volumes:
  db_data:
"""

GOOD_ENV_EXAMPLE = """\
# Example environment for the backend service.
DATABASE_URL=
SECRET_KEY=
LOG_LEVEL=
"""

GOOD_DEPLOY_SH = """\
#!/bin/sh
set -euo pipefail
docker compose build
docker compose up -d
"""

GOOD_DEPLOY_PS1 = """\
# deploy.ps1 — Windows deploy script
$ErrorActionPreference = "Stop"
docker compose build
docker compose up -d
"""

GOOD_README = """\
# My App

## Stack

FastAPI + SQLite.

## Run locally

```
docker compose up
```

## Deploy

Run `./deploy.sh` (POSIX) or `./deploy.ps1` (Windows).
"""


def _good_packaging() -> dict[str, str]:
    """Baseline six-file packaging set; tests override individual entries."""
    return {
        "Dockerfile": GOOD_DOCKERFILE,
        "docker-compose.yml": GOOD_COMPOSE_FASTAPI_SQLITE,
        ".env.example": GOOD_ENV_EXAMPLE,
        "deploy.sh": GOOD_DEPLOY_SH,
        "deploy.ps1": GOOD_DEPLOY_PS1,
        "README.md": GOOD_README,
    }


def _ctx(
    *,
    plan: dict[str, Any] | None = None,
    artifacts_by_layer: dict[str, dict[str, str]] | None = None,
    board: MessageBoard | None = None,
) -> dict[str, Any]:
    nested = dict(artifacts_by_layer or {})
    flat: dict[str, str] = {
        path: content
        for files in nested.values()
        for path, content in files.items()
    }
    return {
        "build_request": BuildRequest(goal="x"),
        "plan": plan or {"stack": {"backend": "fastapi", "database": "sqlite"}},
        "artifacts": flat,
        "artifacts_by_layer": nested,
        "message_board": board,
    }


# ── happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_fastapi_sqlite_emits_all_six_files():
    """FastAPI + SQLite plan → all 6 files produced and validate clean."""
    fake = FakeLMClient(responses=[json.dumps(_good_packaging())])
    stage = PackagerStage(fake)

    ctx = _ctx(
        plan={
            "stack": {"backend": "fastapi", "database": "sqlite"},
            "constraints_satisfied": {"port": 8000},
        },
        artifacts_by_layer={"backend": {"app.py": "from fastapi import FastAPI"}},
    )
    out = await stage.run(ctx)

    # All six files emitted under BOTH views.
    for fname in REQUIRED_FILES:
        assert fname in out["artifacts"], f"missing flat: {fname}"
        assert fname in out["artifacts_by_layer"]["packaging"], f"missing nested: {fname}"

    # Validation: every file ok=True.
    pv = out["package_validation"]
    for fname in REQUIRED_FILES:
        assert pv[fname]["ok"], f"validation failed unexpectedly for {fname}: {pv[fname]['issues']}"

    # System prompt sent verbatim.
    assert fake.calls[0]["messages"][0]["content"] == SYSTEM_PROMPT
    # Exactly one LLM call (no retry needed).
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_python_postgres_stack_validates_postgres_compose():
    """python+postgres → compose file with a postgres service entry."""
    files = _good_packaging()
    files["docker-compose.yml"] = GOOD_COMPOSE_POSTGRES
    fake = FakeLMClient(responses=[json.dumps(files)])
    stage = PackagerStage(fake)

    ctx = _ctx(
        plan={"stack": {"backend": "python", "database": "postgres"}},
        artifacts_by_layer={"backend": {"app.py": "x = 1"}},
    )
    out = await stage.run(ctx)

    # User prompt mentions the postgres image hint.
    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "postgres" in user_msg.lower()
    # Compose validates clean (has a 'db' service).
    assert out["package_validation"]["docker-compose.yml"]["ok"]


@pytest.mark.asyncio
async def test_node_mysql_stack_routes_to_node_base_image():
    """node+mysql → user prompt suggests node:lts-alpine + mysql service."""
    files = _good_packaging()
    files["Dockerfile"] = (
        "FROM node:lts-alpine\n"
        "WORKDIR /app\n"
        "COPY package.json ./\n"
        "RUN npm ci\n"
        "COPY . .\n"
        "EXPOSE 3000\n"
        'HEALTHCHECK CMD wget -qO- http://localhost:3000/health || exit 1\n'
        'CMD ["node", "server.js"]\n'
    )
    fake = FakeLMClient(responses=[json.dumps(files)])
    stage = PackagerStage(fake)

    ctx = _ctx(
        plan={
            "stack": {"backend": "node", "database": "mysql"},
            "constraints_satisfied": {"port": 3000},
        },
        artifacts_by_layer={"backend": {"server.js": "console.log('hi')"}},
    )
    out = await stage.run(ctx)

    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "node:lts-alpine" in user_msg
    assert "mysql" in user_msg.lower()
    assert "EXPOSE port: 3000" in user_msg
    assert out["package_validation"]["Dockerfile"]["ok"]


@pytest.mark.asyncio
async def test_go_stack_no_database_routes_to_golang_base():
    """go (no DB) → user prompt suggests golang:alpine with no DB service."""
    files = _good_packaging()
    files["Dockerfile"] = (
        "FROM golang:alpine AS builder\n"
        "WORKDIR /app\n"
        "COPY . .\n"
        "RUN go build -o app\n"
        "FROM alpine\n"
        "WORKDIR /app\n"
        "COPY --from=builder /app/app /app/\n"
        "EXPOSE 8080\n"
        'HEALTHCHECK CMD wget -qO- http://localhost:8080/health\n'
        'CMD ["/app/app"]\n'
    )
    files["docker-compose.yml"] = (
        "services:\n"
        "  backend:\n"
        "    build: .\n"
        "    ports:\n"
        '      - "8080:8080"\n'
    )
    fake = FakeLMClient(responses=[json.dumps(files)])
    stage = PackagerStage(fake)

    ctx = _ctx(
        plan={"stack": {"backend": "go"}},
        artifacts_by_layer={"backend": {"main.go": "package main"}},
    )
    out = await stage.run(ctx)

    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "golang:alpine" in user_msg
    assert "Database service: none" in user_msg
    assert out["package_validation"]["Dockerfile"]["ok"]


@pytest.mark.asyncio
async def test_rust_stack_no_frontend_routes_to_rust_base():
    """rust (no frontend) → user prompt routes through rust:slim, no nginx."""
    files = _good_packaging()
    fake = FakeLMClient(responses=[json.dumps(files)])
    stage = PackagerStage(fake)

    ctx = _ctx(
        plan={"stack": {"backend": "rust"}},
        artifacts_by_layer={"backend": {"src/main.rs": "fn main() {}"}},
    )
    await stage.run(ctx)

    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "rust:slim" in user_msg
    assert "Frontend nginx runtime stage: no" in user_msg


@pytest.mark.asyncio
async def test_vanilla_python_no_frontend_no_db_uses_python_base():
    """python (no frontend, no DB) → python:3.12-slim, no DB service."""
    files = _good_packaging()
    fake = FakeLMClient(responses=[json.dumps(files)])
    stage = PackagerStage(fake)

    ctx = _ctx(
        plan={"stack": {"backend": "python"}},
        artifacts_by_layer={"backend": {"main.py": "print(1)"}},
    )
    await stage.run(ctx)

    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "python:3.12-slim" in user_msg
    assert "Database service: none" in user_msg
    assert "Frontend nginx runtime stage: no" in user_msg


# ── validator paths ─────────────────────────────────────────────────


def test_validator_catches_missing_from_directive():
    """No FROM directive in the Dockerfile → ok=False with a missing-FROM issue."""
    bad = (
        "WORKDIR /app\n"
        "COPY . .\n"
        "EXPOSE 8000\n"
    )
    res = validator_module.validate_dockerfile(bad)
    assert res.ok is False
    assert any("FROM" in issue for issue in res.issues)


def test_validator_catches_missing_services_key():
    """No services key in compose → ok=False."""
    bad = "version: '3'\nnetworks:\n  default:\n"
    res = validator_module.validate_compose(bad)
    assert res.ok is False
    assert any("services" in issue for issue in res.issues)


def test_validator_catches_potential_secret_in_env_example():
    """A long base64-looking value in .env.example trips the secret heuristic."""
    bad = (
        "DATABASE_URL=\n"
        "# real secret leaked below:\n"
        "API_KEY=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6\n"
    )
    res = validator_module.validate_env_example(bad)
    assert res.ok is False
    assert any("secret" in issue.lower() for issue in res.issues)


def test_validator_catches_missing_shebang_in_deploy_sh():
    """No #! line in deploy.sh → ok=False."""
    bad = "set -e\ndocker compose up -d\n"
    res = validator_module.validate_shell_script(bad, "sh")
    assert res.ok is False
    assert any("shebang" in issue.lower() for issue in res.issues)


def test_validator_catches_missing_strict_mode_in_deploy_ps1():
    """No $ErrorActionPreference in deploy.ps1 → ok=False."""
    bad = "# deploy script\ndocker compose up -d\n"
    res = validator_module.validate_shell_script(bad, "ps1")
    assert res.ok is False
    assert any("ErrorActionPreference" in issue for issue in res.issues)


def test_validator_dockerfile_warns_on_missing_healthcheck_but_ok():
    """No HEALTHCHECK is a warning, not a hard failure — ok stays True."""
    no_health = (
        "FROM python:3.12-slim\n"
        "WORKDIR /app\n"
        "COPY . .\n"
        "EXPOSE 8000\n"
    )
    res = validator_module.validate_dockerfile(no_health)
    assert res.ok is True
    assert any("HEALTHCHECK" in issue for issue in res.issues)


def test_validator_env_example_accepts_placeholders():
    """KEY=<placeholder>, KEY=CHANGEME, etc. don't trip the secret heuristic."""
    ok = (
        "API_KEY=<your-api-key>\n"
        "DB_PASSWORD=CHANGEME\n"
        "TOKEN=YOUR_TOKEN_HERE\n"
        "EMPTY=\n"
    )
    res = validator_module.validate_env_example(ok)
    assert res.ok is True
    assert res.issues == ()


# ── error handling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_garbage_llm_response_records_warning_and_completes(caplog):
    """LLM produces unparseable JSON twice → build records warning, completes.

    Packager is informational; on retry-exhaustion it must NOT raise —
    the build still ships, just with an empty packaging set + a
    warning entry under ``ctx["package_validation"]``.
    """
    fake = FakeLMClient(
        responses=["totally not json", "still nonsense"],
    )
    stage = PackagerStage(fake)

    ctx = _ctx()
    out = await stage.run(ctx)

    # No raise. Validation dict has the synthetic _stage failure entry.
    assert "_stage" in out["package_validation"]
    assert out["package_validation"]["_stage"]["ok"] is False
    # The retry happened (two LLM calls).
    assert len(fake.calls) == 2
    # The retry message includes the strict reminder.
    assert fake.calls[1]["messages"][-1]["content"] == RETRY_REMINDER


@pytest.mark.asyncio
async def test_missing_required_file_recorded_as_validation_failure():
    """LLM omits Dockerfile → validation entry for it has ok=False."""
    files = _good_packaging()
    del files["Dockerfile"]
    fake = FakeLMClient(responses=[json.dumps(files)])
    stage = PackagerStage(fake)

    ctx = _ctx()
    out = await stage.run(ctx)

    pv = out["package_validation"]
    assert pv["Dockerfile"]["ok"] is False
    assert any("not emitted" in issue for issue in pv["Dockerfile"]["issues"])
    # Other files still processed normally.
    assert pv["docker-compose.yml"]["ok"] is True


# ── ctx + serialization tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ctx_artifacts_and_artifacts_by_layer_both_populated():
    """Every emitted file lands in BOTH the flat AND nested views."""
    fake = FakeLMClient(responses=[json.dumps(_good_packaging())])
    stage = PackagerStage(fake)

    ctx = _ctx(
        artifacts_by_layer={"backend": {"app.py": "x"}},
    )
    out = await stage.run(ctx)

    assert "packaging" in out["artifacts_by_layer"]
    for fname in REQUIRED_FILES:
        assert fname in out["artifacts"]
        assert fname in out["artifacts_by_layer"]["packaging"]
        # Same content under both views.
        assert out["artifacts"][fname] == out["artifacts_by_layer"]["packaging"][fname]
    # Upstream backend layer untouched.
    assert out["artifacts_by_layer"]["backend"] == {"app.py": "x"}
    # Backend file still in flat view.
    assert out["artifacts"]["app.py"] == "x"


@pytest.mark.asyncio
async def test_packager_runs_last_in_default_pipeline(
    fake_lm: FakeLMClient, tmp_board: MessageBoard
):
    """Packager is the terminal stage in the v0.5 default pipeline."""
    orch = Orchestrator(fake_lm, tmp_board)
    names = [type(s).__name__ for s in orch.stages]
    assert names[-1] == "PackagerStage"
    assert names == [
        "ArchitectStage",
        "CoderStage",
        "ReviewerStage",
        "TesterStage",
        "PackagerStage",
    ]


@pytest.mark.asyncio
async def test_buildresult_package_validation_visible_in_serialized_output(
    monkeypatch, tmp_board: MessageBoard
):
    """End-to-end: BuildResult.to_dict surfaces package_validation."""
    from development.stages import coder as coder_module

    async def gen_backend(plan, layer, llm):
        return {"app.py": "def f(): return 1"}

    monkeypatch.setattr(
        coder_module, "LAYER_GENERATORS", {"backend": gen_backend}
    )

    plan_json = json.dumps(
        {
            "stack": {"backend": "fastapi"},
            "layers": [
                {"name": "backend", "purpose": "api", "files": ["app.py"]}
            ],
            "dependencies": [],
            "constraints_satisfied": {"port": 8000},
        }
    )

    fake = FakeLMClient(
        responses=[
            plan_json,                                                 # architect
            json.dumps({                                              # reviewer
                "approved": True,
                "issues": [],
                "request_regenerate": False,
            }),
            json.dumps({"test_app.py": "def test(): pass"}),          # tester
            json.dumps(_good_packaging()),                            # packager
        ]
    )

    # Patch the runner so we don't actually spawn pytest.
    from development.stages import _runner as runner_module
    from development.stages import tester as tester_module

    def fake_detect(ws, name, files):
        return None  # no runner -> records as runner_unavailable, no exec

    monkeypatch.setattr(tester_module.runner_module, "detect_runner", fake_detect)

    orch = Orchestrator(fake, tmp_board)
    result = await orch.build(BuildRequest(goal="thing"))

    assert "packager" in result.stages_completed
    # package_validation is populated and serializes cleanly.
    assert result.package_validation
    assert "Dockerfile" in result.package_validation
    d = result.to_dict()
    assert "package_validation" in d
    assert d["package_validation"]["Dockerfile"]["ok"] is True


@pytest.mark.asyncio
async def test_stage_progress_event_published(tmp_board: MessageBoard):
    """Packager emits a STAGE_PROGRESS event with file/validation counts."""
    fake = FakeLMClient(responses=[json.dumps(_good_packaging())])
    stage = PackagerStage(fake)

    ctx = _ctx(board=tmp_board)
    await stage.run(ctx)

    progress = [
        e for e in tmp_board.recent(limit=20) if e.kind == STAGE_PROGRESS
    ]
    assert len(progress) == 1
    payload = progress[0].payload
    assert payload["stage"] == "packager"
    assert payload["files_generated"] == 6
    assert payload["validation_ok"] == 6
    assert payload["validation_failed"] == 0


@pytest.mark.asyncio
async def test_validation_failure_does_not_abort_build(monkeypatch, tmp_board: MessageBoard):
    """A validation failure does NOT mark the build as failed.

    Packager is informational. A Dockerfile missing FROM still results
    in a successful build with the warning recorded — the orchestrator
    must see Packager complete cleanly.
    """
    from development.stages import coder as coder_module

    async def gen_backend(plan, layer, llm):
        return {"app.py": "x = 1"}

    monkeypatch.setattr(
        coder_module, "LAYER_GENERATORS", {"backend": gen_backend}
    )

    bad_files = _good_packaging()
    bad_files["Dockerfile"] = "WORKDIR /app\nEXPOSE 8000\n"  # no FROM!

    plan_json = json.dumps({
        "stack": {"backend": "fastapi"},
        "layers": [{"name": "backend", "purpose": "p", "files": ["app.py"]}],
        "dependencies": [],
        "constraints_satisfied": {},
    })

    fake = FakeLMClient(
        responses=[
            plan_json,
            json.dumps({"approved": True, "issues": [], "request_regenerate": False}),
            json.dumps({"test_app.py": "def test(): pass"}),
            json.dumps(bad_files),
        ]
    )

    from development.stages import tester as tester_module

    def fake_detect(ws, name, files):
        return None

    monkeypatch.setattr(tester_module.runner_module, "detect_runner", fake_detect)

    orch = Orchestrator(fake, tmp_board)
    result = await orch.build(BuildRequest(goal="thing"))

    # Build COMPLETED — packager in stages_completed, no errors.
    assert "packager" in result.stages_completed
    assert result.errors == ()
    # But the Dockerfile validation is recorded as failed.
    assert result.package_validation["Dockerfile"]["ok"] is False


# ── frontend nginx hint ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_react_frontend_triggers_nginx_runtime_hint():
    """react frontend → user prompt says 'Frontend nginx runtime stage: yes'."""
    fake = FakeLMClient(responses=[json.dumps(_good_packaging())])
    stage = PackagerStage(fake)

    ctx = _ctx(
        plan={"stack": {"backend": "fastapi", "frontend": "react"}},
        artifacts_by_layer={
            "backend": {"app.py": "x"},
            "frontend": {"index.html": "<html/>"},
        },
    )
    await stage.run(ctx)

    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "Frontend nginx runtime stage: yes" in user_msg
