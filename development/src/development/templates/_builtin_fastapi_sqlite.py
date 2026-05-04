"""Built-in stack template: FastAPI + SQLite.

The worked-example template. Registered via
``[project.entry-points."development.stack_templates"]`` in
``pyproject.toml`` so the discovery mechanism is exercised against the
same package's own metadata — the simplest case third-party templates
will follow.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..types import BuildRequest
from .base import StackTemplate


class FastApiSqliteTemplate(StackTemplate):
    """FastAPI + SQLite + Docker."""

    name: ClassVar[str] = "fastapi-sqlite"

    def matches(self, stack_hint: str) -> bool:
        """True for hints that mention both FastAPI and SQLite, or the
        canonical ``fastapi-sqlite`` slug.
        """
        h = stack_hint.lower()
        if h == "fastapi-sqlite":
            return True
        if "fastapi" in h and ("sqlite" in h or "sqlite3" in h):
            return True
        return False

    def build_plan(self, request: BuildRequest) -> dict[str, Any]:
        """Return a hand-crafted plan for a FastAPI + SQLite app.

        Same shape the LLM path produces — downstream stages can't tell
        the difference.
        """
        port = request.constraints.get("port", 8000)
        return {
            "stack": {
                "backend": "fastapi",
                "database": "sqlite",
                "deployment": "docker",
            },
            "layers": [
                {
                    "name": "backend",
                    "purpose": "FastAPI HTTP API",
                    "language": "python",
                    "files": ["app/main.py", "app/routes.py"],
                },
                {
                    "name": "database",
                    "purpose": "SQLite persistence via SQLAlchemy",
                    "language": "python",
                    "files": ["app/db.py", "app/models.py"],
                },
                {
                    "name": "deployment",
                    "purpose": "Docker container",
                    "language": "dockerfile",
                    "files": ["Dockerfile", "docker-compose.yml"],
                },
            ],
            "dependencies": ["fastapi", "uvicorn", "sqlalchemy"],
            "constraints_satisfied": {"port": port},
        }
