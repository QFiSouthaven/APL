"""Reasoning Panel persistence + builder.

The panel config is user-editable from the Settings UI and consumed by
the Studio at run time. We store it as a separate TOML file alongside
``settings.toml`` (``config_dir() / "panel.toml"``) so the strict, frozen
:class:`enhancer.config.Settings` dataclass schema does not have to grow
nested-table support.

Schema (TOML)::

    [reasoning_panel]
    enabled = true
    mode = "parallel"
    aggregator = "primary-wins"

    [[reasoning_panel.slots]]
    name = "primary"
    provider = "lmstudio"
    model = "hermes-3-llama-3.1-8b"
    base_url = ""
    role = ""
    weight = 1.0

    [[reasoning_panel.slots]]
    name = "critic"
    provider = "lmstudio"
    model = "qwen3-coder-next"
    base_url = ""
    role = "strict reviewer"
    weight = 1.0

When ``enabled`` is False or no slots are present, the Studio passes
``reasoning_panel=None`` into ``run_pipeline`` and behavior is byte-
identical to v1.x — the panel is opt-in, never auto-on for existing users.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — py3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from ..config import config_dir
from .reasoning_panel import (
    DEFAULT_AGGREGATOR,
    DEFAULT_MODE,
    VALID_AGGREGATORS,
    VALID_MODES,
    LLMSlot,
    ReasoningPanel,
)
from .registry import get_provider as _registry_get_provider

logger = logging.getLogger("enhancer.llm.panel_config")

# Provider names accepted in the slot dropdown — must match
# :func:`enhancer.llm.registry.get_provider` first-class branches.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("lmstudio", "ollama", "openai", "anthropic")


# ─── dataclasses ───────────────────────────────────────────────────────


@dataclass
class SlotConfig:
    """One row in the Settings UI's slots table.

    Mutable so the UI can ``bind_value`` rows directly. Validation
    happens at save / load time, not at attribute set.
    """

    name: str = ""
    provider: str = "lmstudio"
    model: str = ""
    base_url: str = ""
    role: str = ""
    weight: float = 1.0


@dataclass
class PanelConfig:
    """Full reasoning-panel configuration as persisted to ``panel.toml``."""

    enabled: bool = False
    mode: str = DEFAULT_MODE
    aggregator: str = DEFAULT_AGGREGATOR
    slots: list[SlotConfig] = field(default_factory=list)


# ─── path helpers ──────────────────────────────────────────────────────


def panel_config_path() -> Path:
    """Absolute path to ``panel.toml``."""
    return config_dir() / "panel.toml"


# ─── parsing / serialization ───────────────────────────────────────────


def _coerce_slot(raw: Any) -> SlotConfig | None:
    """Coerce a TOML table to a :class:`SlotConfig`. Returns None on failure."""
    if not isinstance(raw, dict):
        return None
    try:
        return SlotConfig(
            name=str(raw.get("name", "")).strip(),
            provider=str(raw.get("provider", "lmstudio")).strip().lower(),
            model=str(raw.get("model", "")).strip(),
            base_url=str(raw.get("base_url", "")).strip(),
            role=str(raw.get("role", "")).strip(),
            weight=float(raw.get("weight", 1.0)),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("Skipping malformed slot %r: %s", raw, exc)
        return None


def from_dict(data: dict[str, Any]) -> PanelConfig:
    """Build a :class:`PanelConfig` from a TOML-style dict.

    Tolerant: unknown keys ignored, malformed slots dropped, type errors
    swallowed in favor of defaults. The Settings UI surfaces validation
    issues separately via :func:`validate`.
    """
    section = data.get("reasoning_panel")
    if not isinstance(section, dict):
        return PanelConfig()

    enabled = bool(section.get("enabled", False))
    mode = str(section.get("mode", DEFAULT_MODE)).strip()
    aggregator = str(section.get("aggregator", DEFAULT_AGGREGATOR)).strip()
    raw_slots = section.get("slots") or []
    if not isinstance(raw_slots, list):
        raw_slots = []

    slots: list[SlotConfig] = []
    for raw in raw_slots:
        slot = _coerce_slot(raw)
        if slot is not None:
            slots.append(slot)

    return PanelConfig(
        enabled=enabled,
        mode=mode if mode in VALID_MODES else DEFAULT_MODE,
        aggregator=aggregator if aggregator in VALID_AGGREGATORS else DEFAULT_AGGREGATOR,
        slots=slots,
    )


def to_dict(cfg: PanelConfig) -> dict[str, Any]:
    """Serialize a :class:`PanelConfig` to a TOML-ready dict."""
    return {
        "reasoning_panel": {
            "enabled": bool(cfg.enabled),
            "mode": cfg.mode,
            "aggregator": cfg.aggregator,
            "slots": [asdict(s) for s in cfg.slots],
        }
    }


def load_panel_config(path: Path | None = None) -> PanelConfig:
    """Read the panel config from ``panel.toml`` (or the supplied path).

    Missing or malformed files yield the default disabled panel — never
    raises. The Settings UI is the only writer; load failures here just
    fall back to "panel disabled" so Studio runs are unaffected.
    """
    target = path or panel_config_path()
    if not target.exists():
        return PanelConfig()
    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Malformed panel TOML at %s: %s — using defaults", target, exc)
        return PanelConfig()
    if not isinstance(data, dict):
        return PanelConfig()
    return from_dict(data)


def save_panel_config(cfg: PanelConfig, path: Path | None = None) -> Path:
    """Write the panel config to disk atomically. Returns the path written."""
    import tomli_w  # lazy: only needed when actually saving

    target = path or panel_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = to_dict(cfg)
    serialized = tomli_w.dumps(payload)

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    return target.resolve()


# ─── validation ────────────────────────────────────────────────────────


def validate(cfg: PanelConfig) -> list[str]:
    """Return a list of human-readable validation errors (empty = valid).

    Validation rules:
      * mode must be a known value
      * aggregator must be a known value
      * if enabled, at least one slot is required (the primary)
      * every slot needs a non-empty name
      * slot names must be unique within a panel
      * slot.provider must be one of :data:`SUPPORTED_PROVIDERS`
      * slot.model must be non-empty when provider is set
    """
    errors: list[str] = []

    if cfg.mode not in VALID_MODES:
        errors.append(
            f"Mode {cfg.mode!r} not in {sorted(VALID_MODES)}"
        )
    if cfg.aggregator not in VALID_AGGREGATORS:
        errors.append(
            f"Aggregator {cfg.aggregator!r} not in {sorted(VALID_AGGREGATORS)}"
        )

    if cfg.enabled and not cfg.slots:
        errors.append("Panel is enabled but has no slots — add at least the primary.")

    seen_names: set[str] = set()
    for i, slot in enumerate(cfg.slots):
        prefix = f"Slot {i} ({slot.name or '<unnamed>'})"
        if not slot.name:
            errors.append(f"{prefix}: name is required.")
        elif slot.name in seen_names:
            errors.append(f"{prefix}: duplicate slot name {slot.name!r}.")
        else:
            seen_names.add(slot.name)

        if slot.provider not in SUPPORTED_PROVIDERS:
            errors.append(
                f"{prefix}: provider {slot.provider!r} not in "
                f"{list(SUPPORTED_PROVIDERS)}"
            )
        if not slot.model:
            errors.append(f"{prefix}: model is required.")

    return errors


# ─── builder ───────────────────────────────────────────────────────────


def _build_provider_for_slot(slot: SlotConfig) -> Any:
    """Construct a ChatProvider for one slot, honoring per-slot ``base_url``.

    Special-case: ``lmstudio`` is the only provider with a real per-slot
    base URL story today; we instantiate :class:`LMStudioProvider`
    directly so the override applies. Other providers fall back to
    :func:`registry.get_provider`, which reads from the active
    :class:`Settings`.
    """
    name = slot.provider.lower().strip()
    if name == "lmstudio":
        # Lazy import — keeps this module testable when LM Studio's
        # heavy deps are unavailable in unit-test envs.
        from .lmstudio import LMStudioProvider
        from ..config import load as load_settings

        settings = load_settings()
        base_url = (slot.base_url or settings.lms_base_url).strip()
        return LMStudioProvider(
            base_url=base_url,
            management_url=settings.lms_management_url,
            default_timeout=settings.request_timeout,
        )

    # For the other three providers, registry.get_provider() reads from
    # Settings and constructs the full client. We pass a synthetic
    # Settings-like dict-or-object only via the live settings to keep
    # behavior consistent with the rest of the app.
    from ..config import load as load_settings

    settings = load_settings()
    # Override the provider name so registry returns the right class.
    # Settings is frozen so we replace via dataclasses.replace.
    from dataclasses import replace
    return _registry_get_provider(replace(settings, provider=name))


def build_panel(cfg: PanelConfig) -> ReasoningPanel | None:
    """Construct a live :class:`ReasoningPanel` from a config, or None.

    Returns None when:
      * the panel is disabled, or
      * validation fails, or
      * no slots are configured.

    Failures are logged at WARNING level — the caller should fall back
    to a no-panel run (byte-identical pre-v2.1 behavior).
    """
    if not cfg.enabled or not cfg.slots:
        return None
    errs = validate(cfg)
    if errs:
        logger.warning("Panel config invalid; ignoring: %s", "; ".join(errs))
        return None

    slots: list[LLMSlot] = []
    for sc in cfg.slots:
        try:
            provider = _build_provider_for_slot(sc)
        except Exception as exc:  # noqa: BLE001 — never break Studio runs
            logger.warning(
                "Failed to construct provider for slot %r: %s — dropping panel",
                sc.name, exc,
            )
            return None
        slots.append(
            LLMSlot(
                name=sc.name,
                provider=provider,
                model=sc.model,
                role=sc.role,
                weight=float(sc.weight),
            )
        )

    if not slots:
        return None
    return ReasoningPanel(slots)


def default_config() -> PanelConfig:
    """A sensible disabled-by-default config the UI seeds new installs with.

    The single primary slot pre-populates the table so users see what the
    schema looks like; ``enabled=False`` keeps Studio runs unchanged.
    """
    return PanelConfig(
        enabled=False,
        mode=DEFAULT_MODE,
        aggregator=DEFAULT_AGGREGATOR,
        slots=[
            SlotConfig(
                name="primary",
                provider="lmstudio",
                model="",
                base_url="",
                role="",
                weight=1.0,
            ),
        ],
    )
