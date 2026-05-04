"""Structural validators for the Packager's emitted files.

Owned by :mod:`development.stages.packager`. Each validator inspects ONE
file's contents (no I/O, no subprocess) and returns a :class:`ValidationResult`
describing the issues found. The Packager records these results in
``ctx["package_validation"]`` and surfaces them via
``BuildResult.package_validation``.

Validation is best-effort and informational. Failures are recorded as
warnings, not gates — the Packager never aborts a build because a file
looks suspicious. The point is to give the user a quick sanity check,
not to second-guess the LLM's output.

The compose validator prefers ``yaml.safe_load`` (PyYAML, available
transitively via ``uvicorn[standard]``). If PyYAML is somehow missing
in a stripped-down environment it falls back to a regex-based smoke
check that looks for a top-level ``services:`` key followed by an
indented service block. The fallback path tags the result with an
informational issue so callers can see which path ran.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# PyYAML is a transitive dep of uvicorn[standard], which is a top-level
# dep in pyproject.toml. We import lazily-tolerantly so a stripped-down
# environment without it falls through to the regex smoke check rather
# than crashing the whole stage.
try:
    import yaml as _yaml  # type: ignore[import-not-found]
    _HAS_YAML = True
except ImportError:  # pragma: no cover — exercised only on a stripped venv
    _yaml = None  # type: ignore[assignment]
    _HAS_YAML = False


@dataclass(frozen=True)
class ValidationResult:
    """One file's validation outcome.

    ``ok`` is False ONLY when a hard-required directive is missing
    (e.g. no FROM in the Dockerfile, no ``services:`` in compose). Soft
    issues (missing HEALTHCHECK, shell-form CMD) leave ``ok=True`` and
    surface as informational entries in ``issues``.

    ``issues`` is always a tuple of human-readable strings; an empty
    tuple means the file passed every check cleanly. Mirrors the shape
    of :class:`development.stages._runner.RunnerResult` so downstream
    consumers can treat them uniformly.
    """

    file: str
    ok: bool
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-friendly dict; tuple → list for serialization."""
        d = asdict(self)
        d["issues"] = list(self.issues)
        return d


# ── Dockerfile ──────────────────────────────────────────────────────


# Top-level instruction names we care about. We match these case-
# insensitively at start-of-line (with optional leading whitespace) so
# multi-stage builds, comments, and blank lines don't confuse detection.
_DOCKER_DIRECTIVE_RE = re.compile(
    r"^\s*(FROM|WORKDIR|COPY|ADD|EXPOSE|HEALTHCHECK|CMD|ENTRYPOINT|RUN)\b",
    re.IGNORECASE | re.MULTILINE,
)


def validate_dockerfile(content: str) -> ValidationResult:
    """Check the Dockerfile for required + recommended directives.

    Hard-required (failure if missing):
        FROM, WORKDIR, COPY or ADD, EXPOSE
    Recommended (warning only):
        HEALTHCHECK
    Discouraged (warning only):
        Shell-form ``CMD some command`` instead of JSON-form
        ``CMD ["some", "command"]`` — the JSON form survives signal
        forwarding cleanly.

    An empty file is always a hard failure.
    """
    if not content or not content.strip():
        return ValidationResult(
            file="Dockerfile",
            ok=False,
            issues=("Dockerfile is empty.",),
        )

    found: set[str] = {
        m.group(1).upper() for m in _DOCKER_DIRECTIVE_RE.finditer(content)
    }
    issues: list[str] = []

    # Hard requirements.
    if "FROM" not in found:
        issues.append("Missing required directive: FROM")
    if "WORKDIR" not in found:
        issues.append("Missing required directive: WORKDIR")
    if "COPY" not in found and "ADD" not in found:
        issues.append("Missing required directive: COPY or ADD")
    if "EXPOSE" not in found:
        issues.append("Missing required directive: EXPOSE")

    hard_failed = bool(issues)

    # Soft recommendations — appended after the hard list so callers
    # filtering by prefix can split them, and so the order is stable.
    if "HEALTHCHECK" not in found:
        issues.append("Recommended: add a HEALTHCHECK directive.")

    # Shell-form CMD detection. JSON-form starts with `[` after the
    # directive name; shell-form does not. We only check non-quoted
    # CMD lines (skip continuations).
    for m in re.finditer(
        r"^\s*CMD\s+(.+)$", content, re.IGNORECASE | re.MULTILINE
    ):
        rest = m.group(1).strip()
        if rest and not rest.startswith("["):
            issues.append(
                "Shell-form CMD detected; prefer JSON-form CMD "
                "[\"executable\", \"arg\"] for proper signal handling."
            )
            break

    return ValidationResult(
        file="Dockerfile",
        ok=not hard_failed,
        issues=tuple(issues),
    )


# ── docker-compose.yml ───────────────────────────────────────────────


# Regex fallback used when PyYAML isn't importable. Matches a top-level
# ``services:`` key followed eventually by an indented service entry.
_COMPOSE_SERVICES_RE = re.compile(
    r"^services:\s*\n"          # top-level key
    r"(?:\s*(?:#.*)?\n)*"       # optional blank/comment lines
    r"^\s+\S",                  # at least one indented non-blank line
    re.MULTILINE,
)


def validate_compose(content: str) -> ValidationResult:
    """Check docker-compose.yml for a parseable services block.

    With PyYAML available (the normal case) we ``yaml.safe_load`` the
    file and verify:
        * Top-level value is a mapping.
        * It has a ``services`` key.
        * ``services`` contains at least one service.
        * That service has either ``image`` or ``build`` set.

    Without PyYAML we fall back to a regex smoke check (``^services:``
    line followed by at least one indented entry) and add an
    informational issue tagging the fallback.
    """
    if not content or not content.strip():
        return ValidationResult(
            file="docker-compose.yml",
            ok=False,
            issues=("docker-compose.yml is empty.",),
        )

    if not _HAS_YAML:
        return _validate_compose_regex_fallback(content)

    try:
        parsed = _yaml.safe_load(content)
    except _yaml.YAMLError as exc:  # type: ignore[attr-defined]
        return ValidationResult(
            file="docker-compose.yml",
            ok=False,
            issues=(f"YAML parse error: {exc}",),
        )

    if not isinstance(parsed, dict):
        return ValidationResult(
            file="docker-compose.yml",
            ok=False,
            issues=("Top-level YAML must be a mapping.",),
        )

    services = parsed.get("services")
    if services is None:
        return ValidationResult(
            file="docker-compose.yml",
            ok=False,
            issues=("Missing required top-level key: services",),
        )
    if not isinstance(services, dict) or not services:
        return ValidationResult(
            file="docker-compose.yml",
            ok=False,
            issues=("`services` must contain at least one service.",),
        )

    # Each service should have image OR build. Soft warning, not failure
    # (compose tolerates oddities like ``extends`` patterns we don't
    # want to gate on here).
    issues: list[str] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            issues.append(f"Service {name!r} must be a mapping.")
            continue
        if "image" not in svc and "build" not in svc:
            issues.append(
                f"Service {name!r} is missing both `image` and `build`."
            )

    return ValidationResult(
        file="docker-compose.yml",
        ok=True,
        issues=tuple(issues),
    )


def _validate_compose_regex_fallback(content: str) -> ValidationResult:
    """PyYAML-free smoke check: looks for ``services:`` + an indented entry."""
    if not _COMPOSE_SERVICES_RE.search(content):
        return ValidationResult(
            file="docker-compose.yml",
            ok=False,
            issues=(
                "Missing top-level `services:` key with at least one "
                "indented service. (PyYAML unavailable — used regex "
                "fallback.)",
            ),
        )
    return ValidationResult(
        file="docker-compose.yml",
        ok=True,
        issues=(
            "PyYAML unavailable — compose validated via regex fallback only.",
        ),
    )


# ── .env.example ─────────────────────────────────────────────────────


# Heuristic patterns for "this looks like a real secret leaked into the
# example file". Each pattern is matched against the value-half (RHS of
# `=`) of a non-comment line. Long base64-ish blobs and big hex strings
# are the typical offenders.
_SECRET_PATTERNS = (
    # Long base64-ish — at least 32 chars from the base64 alphabet.
    re.compile(r"^[A-Za-z0-9+/=_-]{32,}$"),
    # Large hex string — at least 33 hex chars.
    re.compile(r"^[a-fA-F0-9]{33,}$"),
)

# Allowlist of placeholder-ish RHS values that are obviously not real
# secrets even when they're long. The `<...>` form is the most common
# convention; we also let ``CHANGEME`` / ``REPLACE_ME`` / ``YOUR_*``
# pass without flagging.
_PLACEHOLDER_RE = re.compile(
    r"^(?:<[^>]+>|CHANGE_?ME|REPLACE_?ME|YOUR_[A-Z_]+|TODO|EXAMPLE)$",
    re.IGNORECASE,
)


def validate_env_example(content: str) -> ValidationResult:
    """Check .env.example for shape + apparent-secret patterns.

    Each non-blank, non-comment line must be of the form ``KEY=value``.
    A value that matches the secret-patterns regex (long base64 or
    long hex) is flagged as a potential leaked secret.

    Empty values (``KEY=``) are PERMITTED — that's the explicit shape
    the spec asks for ("KEY= placeholders, no values"). Lines without
    ``=`` at all fail the shape check.
    """
    if not content:
        # Empty is technically valid but we'd rather call it out so
        # the reviewer knows nothing got generated.
        return ValidationResult(
            file=".env.example",
            ok=True,
            issues=(".env.example is empty (no keys declared).",),
        )

    issues: list[str] = []
    hard_failed = False

    for lineno, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            issues.append(f"Line {lineno}: not in KEY=value form: {raw!r}")
            hard_failed = True
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            issues.append(f"Line {lineno}: empty key.")
            hard_failed = True
            continue
        if not value:
            # Empty value is the spec's preferred shape. Skip checks.
            continue
        # Strip surrounding quotes before secret-detection so a quoted
        # placeholder doesn't trip the long-string regex.
        unquoted = value
        if (unquoted.startswith('"') and unquoted.endswith('"')) or (
            unquoted.startswith("'") and unquoted.endswith("'")
        ):
            unquoted = unquoted[1:-1]
        if _PLACEHOLDER_RE.match(unquoted):
            continue
        for pat in _SECRET_PATTERNS:
            if pat.match(unquoted):
                issues.append(
                    f"Line {lineno}: value for {key!r} looks like a "
                    "real secret (long base64/hex). .env.example must "
                    "contain placeholders only."
                )
                hard_failed = True
                break

    return ValidationResult(
        file=".env.example",
        ok=not hard_failed,
        issues=tuple(issues),
    )


# ── deploy.sh / deploy.ps1 ───────────────────────────────────────────


def validate_shell_script(content: str, shell: str) -> ValidationResult:
    """Check a deploy script for the expected shebang/strict-mode prelude.

    ``shell='sh'`` requires:
        * A first non-blank line shebang of the form ``#!/bin/sh`` or
          ``#!/usr/bin/env bash`` etc.
        * A ``set -e`` (or ``set -eu``, ``set -euo pipefail``) directive
          near the top of the file.

    ``shell='ps1'`` requires:
        * A leading comment block (line starting with ``#`` OR a block
          comment ``<# ... #>``) — gives the script a recognizable
          header.
        * ``$ErrorActionPreference = "Stop"`` somewhere in the file.

    Mismatched ``shell=`` argument values raise ValueError so the
    Packager fails loudly on a bad call rather than silently
    misvalidating.
    """
    if shell not in ("sh", "ps1"):
        raise ValueError(f"Unsupported shell {shell!r}; expected 'sh' or 'ps1'.")

    file_label = "deploy.sh" if shell == "sh" else "deploy.ps1"

    if not content or not content.strip():
        return ValidationResult(
            file=file_label,
            ok=False,
            issues=(f"{file_label} is empty.",),
        )

    issues: list[str] = []
    hard_failed = False

    if shell == "sh":
        # First non-blank line must be a shebang.
        first = next(
            (line for line in content.splitlines() if line.strip()),
            "",
        )
        if not first.startswith("#!"):
            issues.append("Missing shebang on first non-blank line.")
            hard_failed = True
        elif "/sh" not in first and "/bash" not in first and "bash" not in first:
            # Tolerate ``#!/usr/bin/env bash`` and friends.
            issues.append(
                f"Shebang {first!r} does not reference sh or bash."
            )
            hard_failed = True

        # ``set -e`` (or any strict-mode variant) must appear somewhere
        # before the first non-comment, non-set executable line. We
        # don't enforce the exact position — many real scripts source
        # helpers before ``set -e`` — just that it's present at all.
        if not re.search(r"^\s*set\s+-[eu]", content, re.MULTILINE):
            issues.append(
                "Missing strict-mode directive `set -e` (or `set -eu`)."
            )
            hard_failed = True
    else:
        # PowerShell: ``ps1``.
        first = next(
            (line for line in content.splitlines() if line.strip()),
            "",
        )
        if not (first.lstrip().startswith("#") or first.lstrip().startswith("<#")):
            issues.append("Missing comment-block header on first line.")
            hard_failed = True

        # Strict-mode equivalent: $ErrorActionPreference = "Stop"
        if not re.search(
            r"\$ErrorActionPreference\s*=\s*['\"]Stop['\"]", content
        ):
            issues.append(
                "Missing strict-mode directive `$ErrorActionPreference "
                "= \"Stop\"`."
            )
            hard_failed = True

    return ValidationResult(
        file=file_label,
        ok=not hard_failed,
        issues=tuple(issues),
    )


__all__ = [
    "ValidationResult",
    "validate_dockerfile",
    "validate_compose",
    "validate_env_example",
    "validate_shell_script",
]
