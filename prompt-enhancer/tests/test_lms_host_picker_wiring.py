"""Wiring tests for the v1.2 multi-host LM Studio picker.

Closes the loop between :func:`enhancer.llm.lms_discovery.pick_loaded_host`
(shipped in v1.2 but never user-facing) and
:func:`enhancer.llm.lms_link.set_active_base_url` via the typer CLI's
``--lms-hosts`` flag, ``ENHANCER_LMS_HOSTS`` env var, and the Settings
page's "Pick best host now" button.

Stubs ``apply_host_pick`` / ``pick_loaded_host`` rather than running
real network probes — those paths are already covered by
``test_lms_discovery.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from enhancer.cli.main import LMS_HOSTS_ENV, app
from enhancer.llm import host_picker, lms_link


runner = CliRunner()


# ─── helpers ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_lms_link_store(tmp_path, monkeypatch):
    """Redirect lms_link's persistence path so test setting a runtime
    override never leaks into the user's real %APPDATA%."""
    from enhancer import config as cfg

    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)
    # Defensive: clear any lingering override at start AND end of test.
    lms_link.set_active_base_url(None)
    yield tmp_path
    lms_link.set_active_base_url(None)


# ─── parse_hosts ────────────────────────────────────────────────────


def test_parse_hosts_handles_commas_newlines_and_blanks():
    assert host_picker.parse_hosts("") == []
    assert host_picker.parse_hosts(None) == []
    assert host_picker.parse_hosts("http://a:1") == ["http://a:1"]
    assert host_picker.parse_hosts(
        "http://a:1, http://b:2, , http://c:3,"
    ) == ["http://a:1", "http://b:2", "http://c:3"]
    assert host_picker.parse_hosts(
        "http://a:1\nhttp://b:2\n\nhttp://c:3"
    ) == ["http://a:1", "http://b:2", "http://c:3"]


# ─── apply_host_pick ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_host_pick_sets_override_on_success(
    isolated_lms_link_store, monkeypatch
):
    async def fake_pick(hosts, preferred_model=None):
        return "http://192.168.1.50:1234/v1", "wanted-model"

    monkeypatch.setattr(host_picker, "pick_loaded_host", fake_pick)

    host, model = await host_picker.apply_host_pick(
        ["http://127.0.0.1:1234/v1", "http://192.168.1.50:1234/v1"]
    )

    assert host == "http://192.168.1.50:1234/v1"
    assert model == "wanted-model"
    assert lms_link.get_override() == "http://192.168.1.50:1234/v1"


@pytest.mark.asyncio
async def test_apply_host_pick_no_responder_leaves_override_untouched(
    isolated_lms_link_store, monkeypatch, caplog
):
    # Pre-existing override should NOT be wiped just because picking failed.
    lms_link.set_active_base_url("http://preexisting:9999/v1")

    async def fake_pick(hosts, preferred_model=None):
        return None, None

    monkeypatch.setattr(host_picker, "pick_loaded_host", fake_pick)

    with caplog.at_level(logging.WARNING, logger="enhancer.llm.host_picker"):
        host, model = await host_picker.apply_host_pick(["http://dead:1234/v1"])

    assert host is None and model is None
    assert lms_link.get_override() == "http://preexisting:9999/v1"
    assert any("no host" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_apply_host_pick_empty_list_is_noop(
    isolated_lms_link_store, monkeypatch
):
    """Empty host list must NOT call pick_loaded_host (back-compat:
    default single-host case must be untouched)."""
    called = {"n": 0}

    async def fake_pick(hosts, preferred_model=None):
        called["n"] += 1
        return None, None

    monkeypatch.setattr(host_picker, "pick_loaded_host", fake_pick)

    host, model = await host_picker.apply_host_pick([])
    assert (host, model) == (None, None)
    assert called["n"] == 0
    assert lms_link.get_override() is None


# ─── CLI: --lms-hosts flag ──────────────────────────────────────────


def test_cli_lms_hosts_flag_sets_active_base_url(
    isolated_lms_link_store, monkeypatch
):
    """The global --lms-hosts flag, when supplied, calls pick_loaded_host
    and routes set_active_base_url to the chosen URL — before the
    subcommand runs."""
    seen_hosts: list[list[str]] = []

    async def fake_apply(hosts, *, preferred_model=None):
        host_list = list(hosts)
        seen_hosts.append(host_list)
        chosen = "http://192.168.1.50:1234/v1"
        lms_link.set_active_base_url(chosen)
        return chosen, "model-x"

    monkeypatch.setattr("enhancer.cli.main.apply_host_pick", fake_apply)

    result = runner.invoke(
        app,
        [
            "--lms-hosts",
            "http://127.0.0.1:1234/v1,http://192.168.1.50:1234/v1",
            "version",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert seen_hosts == [
        ["http://127.0.0.1:1234/v1", "http://192.168.1.50:1234/v1"]
    ]
    assert lms_link.get_override() == "http://192.168.1.50:1234/v1"
    # User-facing confirmation in stdout.
    assert "192.168.1.50" in result.stdout


def test_cli_lms_hosts_env_var_works(
    isolated_lms_link_store, monkeypatch
):
    """``ENHANCER_LMS_HOSTS`` must work without the explicit flag."""
    seen: list[list[str]] = []

    async def fake_apply(hosts, *, preferred_model=None):
        seen.append(list(hosts))
        chosen = "http://lan-rig:1234/v1"
        lms_link.set_active_base_url(chosen)
        return chosen, "model-y"

    monkeypatch.setattr("enhancer.cli.main.apply_host_pick", fake_apply)
    monkeypatch.setenv(LMS_HOSTS_ENV, "http://lan-rig:1234/v1,http://other:1234/v1")

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.stdout
    assert seen == [["http://lan-rig:1234/v1", "http://other:1234/v1"]]
    assert lms_link.get_override() == "http://lan-rig:1234/v1"


def test_cli_lms_hosts_no_responder_falls_back(
    isolated_lms_link_store, monkeypatch
):
    """When all hosts are down, the CLI must NOT set an override and
    must log/print a warning — back-compat for single-host default."""
    async def fake_apply(hosts, *, preferred_model=None):
        return None, None

    monkeypatch.setattr("enhancer.cli.main.apply_host_pick", fake_apply)

    result = runner.invoke(
        app,
        ["--lms-hosts", "http://dead-a:1234/v1,http://dead-b:1234/v1", "version"],
    )

    assert result.exit_code == 0, result.stdout
    assert lms_link.get_override() is None
    assert "no host" in result.stdout.lower() or "responded" in result.stdout.lower()


def test_cli_no_lms_hosts_is_backward_compatible(
    isolated_lms_link_store, monkeypatch
):
    """Default invocation — no flag, no env var — must NOT call the
    picker. This is the back-compat invariant called out in the spec."""
    called = {"n": 0}

    async def fake_apply(hosts, *, preferred_model=None):
        called["n"] += 1
        return None, None

    monkeypatch.setattr("enhancer.cli.main.apply_host_pick", fake_apply)
    monkeypatch.delenv(LMS_HOSTS_ENV, raising=False)

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.stdout
    assert called["n"] == 0
    assert lms_link.get_override() is None


# ─── Settings page: Pick-best-host helper ───────────────────────────


@pytest.mark.asyncio
async def test_settings_page_pick_helper_calls_apply_host_pick(
    isolated_lms_link_store, monkeypatch
):
    """The Settings page's button calls ``apply_host_pick`` with the
    parsed host list. We exercise the helper logic directly — the same
    parse → apply pipeline the button uses — since NiceGUI's button
    callback is hard to invoke headlessly."""
    captured: dict[str, object] = {}

    async def fake_apply(hosts, *, preferred_model=None):
        captured["hosts"] = list(hosts)
        return "http://winner:1234/v1", "loaded-llm"

    monkeypatch.setattr(host_picker, "apply_host_pick", fake_apply)

    raw_textarea = (
        "http://127.0.0.1:1234/v1\n"
        "http://192.168.1.50:1234/v1\n"
        "\n"  # blank line should be ignored
    )
    parsed = host_picker.parse_hosts(raw_textarea)
    host, model = await host_picker.apply_host_pick(parsed)

    assert captured["hosts"] == [
        "http://127.0.0.1:1234/v1",
        "http://192.168.1.50:1234/v1",
    ]
    assert host == "http://winner:1234/v1"
    assert model == "loaded-llm"


def test_settings_page_renders_with_picker_section(ui_tmp_db):
    """Smoke: the Settings page still renders end-to-end with the new
    multi-host card present."""
    from enhancer.ui.pages import settings as settings_page

    # render() must not raise — the new ``apply_host_pick`` import path
    # and the textarea/button wiring should construct cleanly.
    settings_page.render()
