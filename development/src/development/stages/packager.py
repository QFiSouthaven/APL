"""Packager — stage 5: emit deployment artifacts for the build.

The Packager runs LAST in the default v0.5 pipeline. It reads the
Architect's plan + the per-layer artifacts produced upstream and asks
the LLM to render six packaging files:

    Dockerfile           — multistage when the stack supports it
    docker-compose.yml   — services for backend + DB if present
    .env.example         — sanitized; KEY= placeholders only
    deploy.sh            — POSIX shell deploy script
    deploy.ps1           — PowerShell deploy script (CRLF preserved)
    README.md            — overview + how-to-run + deploy steps

Each file lands in BOTH ``ctx["artifacts"]`` (flat) and
``ctx["artifacts_by_layer"]["packaging"]`` (nested), matching the dual
view the Coder maintains for upstream layers.

After emission the Packager validates each file structurally via
:mod:`._packager_validator`. Validation results land in
``ctx["package_validation"]`` (file-path → ValidationResult.to_dict())
and propagate to :class:`BuildResult.package_validation`. Crucially,
**validation failures are warnings, not gates** — the build always
completes once Packager runs. This is consistent with the framework's
"Packager doesn't fail builds for cosmetic issues" stance documented
in ``docs/DEVELOPMENT_FRAMEWORK.md`` §4.

Stack awareness is hybrid-driven: the system prompt is fixed (so tests
can byte-match it) but the user prompt embeds a stack-derived hint
table that nudges the LLM toward the right base image, build command,
and database service. Any stack we don't have a dedicated row for
falls through to a generic ``python:3.12-slim`` single-stage with a
TODO comment — better than guessing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from .._json_utils import parse_llm_json
from ..messageboard import MessageBoard
from ..types import STAGE_PROGRESS, LayerGenerationError
from ._packager_validator import (
    ValidationResult,
    validate_compose,
    validate_dockerfile,
    validate_env_example,
    validate_shell_script,
)
from .base import Stage

logger = logging.getLogger("development.stages.packager")


# Pinned so tests can assert byte-for-byte. Kept as a module-level
# constant rather than inlining so prompt experiments are a one-line
# diff. The required-files list and the Dockerfile constraints are
# baked into the prompt because the validator enforces them — keeping
# the LLM's instructions in lockstep with the validator avoids a class
# of "I told it the wrong thing and the validator rejected the result"
# bugs.
SYSTEM_PROMPT = (
    "You are a release engineer. Given the build plan and the source-file "
    "path list, produce JSON ONLY mapping packaging file paths to their "
    "full content. Required files: Dockerfile, docker-compose.yml, "
    ".env.example, deploy.sh, deploy.ps1, README.md. The Dockerfile MUST: "
    "(a) use a multistage build when the stack supports it; (b) expose "
    "the port from plan.constraints_satisfied.port (or 8000 if absent); "
    "(c) set WORKDIR /app; (d) include a HEALTHCHECK directive. The "
    ".env.example MUST contain only KEY= placeholders, no real values. "
    "The shell scripts MUST start with the appropriate shebang and "
    "`set -e` (or PowerShell strict mode). Output only valid JSON, no "
    "prose."
)


# Strict-retry reminder appended after a parse failure.
RETRY_REMINDER = (
    "Your previous response could not be parsed as JSON. Output ONLY a "
    "valid JSON object whose keys are packaging file paths and whose "
    "values are the file contents as strings. No markdown fences, no "
    "commentary."
)


# Stack → (base_image, build_command_hint). This is the lookup the user
# prompt embeds so the LLM has a concrete suggestion. The validator
# only enforces directive-shape correctness, not which image got picked.
_BACKEND_BASES: dict[str, tuple[str, str]] = {
    "python": ("python:3.12-slim", "multistage with venv copy"),
    "fastapi": ("python:3.12-slim", "multistage with venv copy"),
    "flask": ("python:3.12-slim", "multistage with venv copy"),
    "django": ("python:3.12-slim", "multistage with venv copy"),
    "node": ("node:lts-alpine", "npm ci"),
    "express": ("node:lts-alpine", "npm ci"),
    "nestjs": ("node:lts-alpine", "npm ci"),
    "go": ("golang:alpine", "multistage with `go build`"),
    "rust": ("rust:slim", "multistage with `cargo build --release`"),
}

_GENERIC_BASE: tuple[str, str] = (
    "python:3.12-slim",
    "generic single-stage with TODO comment",
)


# Frontend stacks that warrant a separate nginx-served runtime stage.
_FRONTEND_NGINX_STACKS: frozenset[str] = frozenset({
    "vite", "react", "vue", "svelte",
})


# Database stacks that warrant a docker-compose service entry.
_DB_SERVICES: dict[str, str] = {
    "postgres": "postgres:16-alpine",
    "postgresql": "postgres:16-alpine",
    "mysql": "mysql:8",
    "mongodb": "mongo:7",
    "mongo": "mongo:7",
}


# The filenames the Packager declares as required output. The prompt
# instructs the LLM to emit all six; if any are missing after parsing
# we still record what we got, but the validator will flag the gaps.
REQUIRED_FILES: tuple[str, ...] = (
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    "deploy.sh",
    "deploy.ps1",
    "README.md",
)


class PackagerStage(Stage):
    """Stage 5: emit deployment artifacts; validate them structurally.

    Packager is INFORMATIONAL, not a gate — a build with validation
    warnings still completes successfully. The validation results are
    surfaced in ``BuildResult.package_validation`` for the user.
    """

    name: ClassVar[str] = "packager"

    async def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        plan: dict[str, Any] = ctx.get("plan") or {}
        artifacts_by_layer: dict[str, dict[str, str]] = ctx.setdefault(
            "artifacts_by_layer", {}
        )
        artifacts_flat: dict[str, str] = ctx.setdefault("artifacts", {})
        board: MessageBoard | None = ctx.get("message_board")

        # 1. Drive the LLM with a stack-aware prompt; tolerate one retry.
        try:
            files = await self._generate_packaging_files(plan, artifacts_by_layer)
        except LayerGenerationError as exc:
            logger.warning(
                "Packager: LLM produced unparseable JSON after retry; "
                "recording empty package_validation with the failure. "
                "Issue: %s",
                exc,
            )
            ctx["package_validation"] = {
                "_stage": {
                    "file": "_stage",
                    "ok": False,
                    "issues": [f"Packager LLM failed to produce JSON: {exc}"],
                }
            }
            self._publish_progress(board, generated=0, ok_count=0, fail_count=1)
            return ctx

        # 2. Mount each generated file into BOTH artifact views. The
        #    Packager produces files under "packaging" so the
        #    by-layer view stays consistent with how Coder lays out
        #    the upstream layers.
        packaging = artifacts_by_layer.setdefault("packaging", {})
        for path, content in files.items():
            packaging[path] = content
            artifacts_flat[path] = content

        # 3. Validate each known-shape file. Unknown paths are skipped
        #    silently — the LLM may emit extras like LICENSE that we
        #    don't structurally validate.
        validation = self._validate_files(files)
        ctx["package_validation"] = {
            res.file: res.to_dict() for res in validation
        }

        ok_count = sum(1 for r in validation if r.ok)
        fail_count = len(validation) - ok_count
        self._publish_progress(
            board,
            generated=len(files),
            ok_count=ok_count,
            fail_count=fail_count,
        )
        return ctx

    # ── internal helpers ────────────────────────────────────────────

    async def _generate_packaging_files(
        self,
        plan: dict[str, Any],
        artifacts_by_layer: dict[str, dict[str, str]],
    ) -> dict[str, str]:
        """Drive the one-retry LLM-call loop for the packaging file set.

        Returns the parsed ``{path: content}`` dict on success.
        Raises :class:`LayerGenerationError` if both attempts fail to
        parse.
        """
        user_prompt = _build_user_prompt(plan, artifacts_by_layer)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        raw = await self._llm.chat(messages, temperature=0.2, max_tokens=8192)
        parsed = parse_llm_json(raw)

        if parsed is None:
            logger.warning(
                "Packager: first response failed to parse; retrying once. "
                "Raw=%r",
                raw[:200],
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": raw},
                {"role": "user", "content": RETRY_REMINDER},
            ]
            raw = await self._llm.chat(messages, temperature=0.0, max_tokens=8192)
            parsed = parse_llm_json(raw)

        if parsed is None:
            raise LayerGenerationError("packager", raw_response=raw)

        # Coerce: keys must be str, values must be str. Mirror the
        # ``layers/_common`` shape — drop non-str keys, stringify
        # non-str values.
        result: dict[str, str] = {}
        for path, content in parsed.items():
            if not isinstance(path, str):
                continue
            if isinstance(content, str):
                result[path] = content
            else:
                result[path] = json.dumps(content, indent=2)
        return result

    def _validate_files(
        self, files: dict[str, str]
    ) -> list[ValidationResult]:
        """Run each shape validator against the matching emitted file.

        Required files that are missing produce a synthetic
        ``ok=False`` result so the user sees the gap in
        ``package_validation``. Files we don't recognize (LICENSE,
        CHANGELOG, etc.) are silently skipped.
        """
        out: list[ValidationResult] = []

        for required in REQUIRED_FILES:
            content = files.get(required)
            if content is None:
                out.append(
                    ValidationResult(
                        file=required,
                        ok=False,
                        issues=(f"Required file {required!r} was not emitted.",),
                    )
                )
                continue
            if required == "Dockerfile":
                out.append(validate_dockerfile(content))
            elif required == "docker-compose.yml":
                out.append(validate_compose(content))
            elif required == ".env.example":
                out.append(validate_env_example(content))
            elif required == "deploy.sh":
                out.append(validate_shell_script(content, "sh"))
            elif required == "deploy.ps1":
                out.append(validate_shell_script(content, "ps1"))
            else:
                # README.md and any future required file we don't have
                # a structural validator for: just record presence.
                out.append(
                    ValidationResult(
                        file=required,
                        ok=True,
                        issues=(),
                    )
                )

        return out

    def _publish_progress(
        self,
        board: MessageBoard | None,
        *,
        generated: int,
        ok_count: int,
        fail_count: int,
    ) -> None:
        if board is None:
            return
        board.publish(
            STAGE_PROGRESS,
            {
                "stage": self.name,
                "files_generated": generated,
                "validation_ok": ok_count,
                "validation_failed": fail_count,
            },
        )


# ── module-level helpers ────────────────────────────────────────────


def _build_user_prompt(
    plan: dict[str, Any],
    artifacts_by_layer: dict[str, dict[str, str]],
) -> str:
    """Render the packaging user-message body.

    The prompt embeds:
        * The stack hints from ``plan["stack"]``.
        * The chosen base image + build approach for the backend.
        * Whether a frontend nginx stage is warranted.
        * Whether a DB service entry is warranted (and which image).
        * The flat list of source-file paths the build produced.
        * The required-files list — repeated even though the system
          prompt covers it, because LLMs reliably miss items unless
          they appear in BOTH the system and user prompts.
    """
    stack = plan.get("stack") or {}
    backend = str(stack.get("backend") or "").lower()
    frontend = str(stack.get("frontend") or "").lower()
    database = str(stack.get("database") or "").lower()

    base_image, build_hint = _BACKEND_BASES.get(backend, _GENERIC_BASE)
    needs_nginx = frontend in _FRONTEND_NGINX_STACKS
    db_image = _DB_SERVICES.get(database)

    port = (
        plan.get("constraints_satisfied", {}).get("port")
        if isinstance(plan.get("constraints_satisfied"), dict)
        else None
    )
    port = port or 8000

    paths = sorted({
        path
        for layer_files in artifacts_by_layer.values()
        for path in layer_files.keys()
    })
    path_lines = [f"  {p}" for p in paths] if paths else ["  (none)"]

    parts: list[str] = [
        "Build plan:",
        json.dumps(plan, indent=2),
        "",
        "Stack-derived packaging hints:",
        f"  Backend base image: {base_image}",
        f"  Backend build approach: {build_hint}",
        f"  Frontend nginx runtime stage: {'yes' if needs_nginx else 'no'}",
        f"  Database service: {db_image or 'none'}",
        f"  EXPOSE port: {port}",
        "",
        "Source-file paths from upstream layers:",
        *path_lines,
        "",
        "Emit the six required files: Dockerfile, docker-compose.yml, "
        ".env.example, deploy.sh, deploy.ps1, README.md.",
    ]
    return "\n".join(parts)


__all__ = [
    "PackagerStage",
    "SYSTEM_PROMPT",
    "RETRY_REMINDER",
    "REQUIRED_FILES",
]
