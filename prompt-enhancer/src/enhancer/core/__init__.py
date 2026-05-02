"""Core pipeline — pure async, no transport dependency.

Public surface:

* ``run_pipeline(prompt, opts, on_event)`` — the 4-pass enhancer.
* ``EventType`` — frozen event-name enum.
* ``PipelineOptions`` — typed knobs (temperature, modes, session_id, ...).
"""
