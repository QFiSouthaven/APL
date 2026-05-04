"""Local LLM-driven app builder.

The 4th product in the APL umbrella, alongside ``prompt-enhancer``,
``round-robin``, and ``hardware-info``. Drives a local LLM (LM Studio)
through staged code generation: Architect → Coder → Reviewer → Tester
→ Packager. Stages run on top of a SQLite-backed message board so peer
products (round-robin in particular) can subscribe to progress events.

Discovery key: ``"development"``. Default URL: ``http://127.0.0.1:8767``.
The umbrella's ``services.toml`` already wires that key into all peers.
"""

from __future__ import annotations

__version__ = "2.0.0"
