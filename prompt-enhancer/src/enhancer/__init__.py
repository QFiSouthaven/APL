"""prompt-enhancer — Local Desktop Studio for multi-pass AI prompt enhancement.

The public API is intentionally tiny: most callers use the CLI, the UI, or
``run_pipeline`` directly. Provider implementations live under
``enhancer.llm`` and persistence under ``enhancer.persistence``.
"""

__version__ = "1.0.0"
