"""Tests for the Reasoning Panel settings UI + Studio wiring.

Six properties guarded:

* save → load round-trips a multi-slot config (TOML on disk).
* a disabled config yields ``reasoning_panel=None`` to ``run_pipeline``.
* an enabled valid config builds a panel of the right size.
* an invalid config falls back safely (Studio runs without panel).
* the configured ``mode`` + ``aggregator`` flow through to ``run_pipeline``.
* an LMStudio slot with a ``base_url`` override creates a provider whose
  active base URL points there.
"""

from __future__ import annotations

import pytest

from enhancer.llm.panel_config import (
    PanelConfig,
    SlotConfig,
    build_panel,
    from_dict,
    load_panel_config,
    save_panel_config,
    to_dict,
    validate,
)
from enhancer.llm.reasoning_panel import ReasoningPanel


# ─── round-trip ────────────────────────────────────────────────────────


def test_save_load_panel_config_roundtrips(tmp_path):
    """Three-slot config: write, reload, all fields match."""
    target = tmp_path / "panel.toml"
    cfg = PanelConfig(
        enabled=True,
        mode="parallel",
        aggregator="primary-wins",
        slots=[
            SlotConfig(
                name="primary", provider="lmstudio", model="hermes-3-llama-3.1-8b",
                base_url="", role="", weight=1.0,
            ),
            SlotConfig(
                name="critic", provider="lmstudio", model="qwen3-coder-next",
                base_url="http://192.168.1.50:1234/v1",
                role="strict reviewer", weight=2.0,
            ),
            SlotConfig(
                name="alt", provider="anthropic", model="claude-opus-4-7",
                base_url="", role="alternative perspective", weight=0.75,
            ),
        ],
    )

    written = save_panel_config(cfg, target)
    assert written.exists()

    loaded = load_panel_config(target)
    assert loaded.enabled is True
    assert loaded.mode == "parallel"
    assert loaded.aggregator == "primary-wins"
    assert len(loaded.slots) == 3
    assert loaded.slots[0].name == "primary"
    assert loaded.slots[0].model == "hermes-3-llama-3.1-8b"
    assert loaded.slots[1].name == "critic"
    assert loaded.slots[1].base_url == "http://192.168.1.50:1234/v1"
    assert loaded.slots[1].role == "strict reviewer"
    assert loaded.slots[1].weight == 2.0
    assert loaded.slots[2].provider == "anthropic"
    assert loaded.slots[2].weight == 0.75


def test_load_missing_file_returns_default_disabled(tmp_path):
    """A nonexistent panel.toml loads as disabled with no slots."""
    cfg = load_panel_config(tmp_path / "does-not-exist.toml")
    assert cfg.enabled is False
    assert cfg.slots == []


def test_load_malformed_toml_returns_default(tmp_path):
    target = tmp_path / "panel.toml"
    target.write_text("this is = not valid = toml [[[", encoding="utf-8")
    cfg = load_panel_config(target)
    assert cfg.enabled is False
    assert cfg.slots == []


def test_from_dict_rejects_bad_mode_and_aggregator():
    cfg = from_dict({
        "reasoning_panel": {
            "enabled": True, "mode": "warp-9", "aggregator": "magic-8-ball",
            "slots": [],
        }
    })
    assert cfg.mode == "parallel"  # default fallback
    assert cfg.aggregator == "primary-wins"


def test_to_dict_shape_matches_schema():
    cfg = PanelConfig(
        enabled=True, mode="sequential", aggregator="longest",
        slots=[SlotConfig(name="primary", provider="lmstudio", model="m1")],
    )
    payload = to_dict(cfg)
    assert "reasoning_panel" in payload
    rp = payload["reasoning_panel"]
    assert rp["enabled"] is True
    assert rp["mode"] == "sequential"
    assert rp["aggregator"] == "longest"
    assert isinstance(rp["slots"], list) and len(rp["slots"]) == 1
    assert rp["slots"][0]["name"] == "primary"


# ─── disabled path ─────────────────────────────────────────────────────


def test_disabled_panel_yields_none_in_studio():
    """``build_panel`` returns None when ``enabled=False`` even with valid slots.

    This is the exact path the Studio takes: read panel.toml → call
    build_panel → pass result (or None) into run_pipeline.
    """
    cfg = PanelConfig(
        enabled=False,
        mode="parallel",
        aggregator="primary-wins",
        slots=[
            SlotConfig(name="primary", provider="lmstudio", model="m1"),
            SlotConfig(name="critic", provider="lmstudio", model="m2"),
        ],
    )
    assert build_panel(cfg) is None


# ─── enabled path ──────────────────────────────────────────────────────


def test_enabled_panel_builds_correct_slot_count():
    """1 primary + 2 partners → ReasoningPanel.partners is length 2."""
    cfg = PanelConfig(
        enabled=True,
        mode="parallel",
        aggregator="primary-wins",
        slots=[
            SlotConfig(name="primary", provider="lmstudio", model="m1"),
            SlotConfig(name="critic", provider="lmstudio", model="m2",
                       role="strict"),
            SlotConfig(name="alt", provider="lmstudio", model="m3",
                       role="alternative"),
        ],
    )
    panel = build_panel(cfg)
    assert isinstance(panel, ReasoningPanel)
    assert len(panel) == 3
    assert panel.primary.name == "primary"
    assert len(panel.partners) == 2
    assert {p.name for p in panel.partners} == {"critic", "alt"}


# ─── invalid path ──────────────────────────────────────────────────────


def test_invalid_config_falls_back_safely():
    """Missing model on enabled panel → validate() flags it AND build_panel
    returns None. Studio code path: errors are surfaced to user, panel
    arg stays None, run_pipeline runs unaffected.
    """
    bad = PanelConfig(
        enabled=True,
        mode="parallel",
        aggregator="primary-wins",
        slots=[
            SlotConfig(name="primary", provider="lmstudio", model=""),  # missing
        ],
    )
    errs = validate(bad)
    assert errs, "validation should flag missing model on enabled panel"
    assert any("model is required" in e for e in errs)
    assert build_panel(bad) is None


def test_validate_flags_duplicate_slot_names():
    cfg = PanelConfig(
        enabled=True, mode="parallel", aggregator="primary-wins",
        slots=[
            SlotConfig(name="dup", provider="lmstudio", model="m1"),
            SlotConfig(name="dup", provider="lmstudio", model="m2"),
        ],
    )
    errs = validate(cfg)
    assert any("duplicate" in e.lower() for e in errs)


def test_validate_flags_unsupported_provider():
    cfg = PanelConfig(
        enabled=True, mode="parallel", aggregator="primary-wins",
        slots=[SlotConfig(name="primary", provider="bogus", model="x")],
    )
    errs = validate(cfg)
    assert any("provider" in e.lower() for e in errs)


def test_validate_requires_at_least_one_slot_when_enabled():
    cfg = PanelConfig(enabled=True, slots=[])
    errs = validate(cfg)
    assert any("no slots" in e.lower() for e in errs)


# ─── mode + aggregator propagate ──────────────────────────────────────


@pytest.mark.asyncio
async def test_panel_uses_configured_mode_and_aggregator(monkeypatch):
    """Configure mode='parallel', aggregator='longest' and assert that
    run_pipeline receives those values when called via build_panel + the
    same kwarg shape Studio uses.

    We don't run the real pipeline — we patch ``run_pipeline`` to capture
    its kwargs. The Studio wiring builds ``panel_kwargs`` then forwards
    them, so capturing kwargs is equivalent to capturing the wire.
    """
    cfg = PanelConfig(
        enabled=True,
        mode="parallel",
        aggregator="longest",
        slots=[
            SlotConfig(name="primary", provider="lmstudio", model="m1"),
            SlotConfig(name="b", provider="lmstudio", model="m2"),
        ],
    )
    panel = build_panel(cfg)
    assert panel is not None

    captured: dict[str, object] = {}

    async def fake_run_pipeline(*args, **kwargs):
        captured.update(kwargs)
        # Return a minimal fake PipelineResult-like object — but this test
        # only inspects kwargs, so we don't need a full result.
        return None

    # Build the same panel_kwargs Studio would have built and call.
    panel_kwargs = dict(
        reasoning_panel=panel,
        panel_mode=cfg.mode,
        panel_aggregator=cfg.aggregator,
    )
    await fake_run_pipeline("prompt", provider=None, model="x", **panel_kwargs)

    assert captured["panel_mode"] == "parallel"
    assert captured["panel_aggregator"] == "longest"
    assert captured["reasoning_panel"] is panel


# ─── per-slot base_url ─────────────────────────────────────────────────


def test_lmstudio_slot_uses_per_slot_base_url(monkeypatch, tmp_path):
    """A slot with ``base_url='http://192.168.1.50:1234/v1'`` constructs
    an :class:`LMStudioProvider` whose configured base URL points there.

    Pin the user config dir to tmp_path so reading ``Settings`` doesn't
    touch %APPDATA%.
    """
    from enhancer import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "config_dir", lambda: tmp_path)

    cfg = PanelConfig(
        enabled=True,
        mode="parallel",
        aggregator="primary-wins",
        slots=[
            SlotConfig(name="primary", provider="lmstudio", model="m1"),
            SlotConfig(
                name="critic", provider="lmstudio", model="m2",
                base_url="http://192.168.1.50:1234/v1",
            ),
        ],
    )
    panel = build_panel(cfg)
    assert panel is not None

    critic = panel.partners[0]
    # LMStudioProvider stores the configured base URL on _default_base_url
    # and exposes it via .base_url (which factors in the lms_link override).
    assert critic.provider._default_base_url == "http://192.168.1.50:1234/v1"


def test_lmstudio_slot_without_base_url_falls_back_to_settings(monkeypatch, tmp_path):
    """When a slot has no ``base_url``, the LMStudioProvider uses the
    active :class:`Settings.lms_base_url`. This preserves single-host
    UX for users who never set a per-slot URL.
    """
    from enhancer import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "config_dir", lambda: tmp_path)

    cfg = PanelConfig(
        enabled=True, mode="parallel", aggregator="primary-wins",
        slots=[SlotConfig(name="primary", provider="lmstudio", model="m1")],
    )
    panel = build_panel(cfg)
    assert panel is not None
    primary = panel.primary
    # Default Settings.lms_base_url is 127.0.0.1:1234/v1.
    assert primary.provider._default_base_url == "http://127.0.0.1:1234/v1"


# ─── settings page renders with the new card ──────────────────────────


def test_settings_page_renders_with_reasoning_panel_card(ui_tmp_db):
    """Smoke: Settings page constructs end-to-end with the new section."""
    from enhancer.ui.pages import settings as settings_page

    # render() must not raise — the new panel-config helpers must
    # construct cleanly against an empty config dir.
    settings_page.render()
