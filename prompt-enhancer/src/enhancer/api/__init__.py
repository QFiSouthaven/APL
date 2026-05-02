"""HTTP REST adapter for inter-product integration.

Exposes the enhancer pipeline as a versioned JSON-over-HTTP contract so
sibling products (round-robin, interpreter, swarm-loop) can call into
it without importing this package. See ``docs/INTEGRATION.md`` for the
shared envelope schema and per-product contracts.
"""

ENVELOPE_SCHEMA_VERSION = "1.0"
