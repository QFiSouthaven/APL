"""Coder-stage tool catalog (v2.0 MCP-style integration).

This subpackage exposes a tiny, intentionally narrow tool surface that the
Coder stage can offer to a tool-aware LLM call when ``tool_use=True`` is
opted in. The shape mirrors OpenAI's function-calling format so any model
the LMStudioProvider speaks to can emit ``tool_calls``; we then dispatch
them locally without needing a real MCP server.

Three categories, seven functions:

* Filesystem (read-only):  :func:`fs_read`, :func:`fs_list`
* Git (read-only):         :func:`git_status`, :func:`git_log`, :func:`git_diff`
* Sandboxed execution:     :func:`sandboxed_exec`

All filesystem and exec calls require a ``sandbox_dir`` argument that
constrains the operations to a single temp directory; path-traversal is
rejected by resolving and re-checking against the sandbox root.

The :data:`MAX_TOOL_CALLS_PER_LAYER` constant is the default budget for a
single layer's generator call. The Coder will force a final response after
that many tool calls, regardless of whether the LLM tries to keep going.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .exec import sandboxed_exec
from .filesystem import fs_list, fs_read
from .git import git_diff, git_log, git_status

# Default cap on tool calls per layer generator invocation. Configurable
# by passing ``tool_call_budget=N`` to :class:`development.stages.coder.CoderStage`.
MAX_TOOL_CALLS_PER_LAYER: int = 5


# OpenAI-format tool definitions. The Coder forwards this catalog to the
# LLM verbatim; the LLM emits ``tool_calls`` referring to these by name.
TOOL_CATALOG: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "fs_read",
            "description": "Read a text file from the sandbox dir. Returns content (decoded UTF-8).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the sandbox dir.",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "Cap on bytes read (default 65536).",
                        "default": 65536,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_list",
            "description": "List immediate entries in a directory inside the sandbox dir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path (default '.').",
                        "default": ".",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Run `git status --porcelain` in a repo dir under the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_dir": {"type": "string", "default": "."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Run `git log` (read-only) in a repo dir under the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_dir": {"type": "string", "default": "."},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Run `git diff` (or `git diff --staged`) in a repo dir under the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_dir": {"type": "string", "default": "."},
                    "staged": {"type": "boolean", "default": False},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandboxed_exec",
            "description": (
                "Spawn a subprocess inside the sandbox dir. argv[0] basename "
                "must be in the allowed list (python, node, go, cargo, "
                "pytest, vitest, npm, echo, ls, cat). Times out by default at 30s."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Argv list. argv[0] is the binary; rest are args.",
                    },
                    "cwd": {"type": "string", "default": "."},
                    "timeout_s": {"type": "number", "default": 30.0},
                },
                "required": ["cmd"],
            },
        },
    },
]


# Runtime dispatch: name → coroutine. Each function takes its own kwargs
# plus ``sandbox_dir``; the caller (``dispatch_tool_call``) injects sandbox_dir.
TOOL_DISPATCH: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "fs_read": fs_read,
    "fs_list": fs_list,
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    "sandboxed_exec": sandboxed_exec,
}


async def dispatch_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    sandbox_dir: Any,
) -> dict[str, Any]:
    """Look up ``tool_name`` and invoke it with ``**arguments`` + sandbox_dir.

    Returns ``{ok: false, error: "unknown_tool", ...}`` if the name isn't
    in the catalog. Argument-validation errors (missing required keys,
    wrong types) are caught and surfaced uniformly so the LLM sees a
    consistent error envelope and can recover.
    """
    impl = TOOL_DISPATCH.get(tool_name)
    if impl is None:
        return {"ok": False, "error": "unknown_tool", "tool": tool_name}
    try:
        return await impl(sandbox_dir=sandbox_dir, **dict(arguments or {}))
    except TypeError as exc:
        return {"ok": False, "error": "bad_arguments", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 — uniform envelope for the LLM
        return {"ok": False, "error": "tool_exception", "detail": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "MAX_TOOL_CALLS_PER_LAYER",
    "TOOL_CATALOG",
    "TOOL_DISPATCH",
    "dispatch_tool_call",
    "fs_read",
    "fs_list",
    "git_status",
    "git_log",
    "git_diff",
    "sandboxed_exec",
]
