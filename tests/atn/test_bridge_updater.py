"""Bridge npm-dependency updater (atn/providers/bridge_updater.py).

Everything runs against temp dirs: a fake packaged-sources dir, a fake
~/.atn/bridge home, a fake installer (writes node_modules package.json files
at the pinned versions), a fake registry and a fake probe. The real
~/.atn is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import atn.providers.bridge as bridgemod
from atn.providers import bridge_updater as bu
from atn.providers.bridge_updater import (
    BridgeUpdater,
    caret_bounds,
    max_satisfying,
    satisfies,
)

SDK = "@anthropic-ai/claude-agent-sdk"
CC = "@anthropic-ai/claude-code"
CODEX = "@openai/codex-sdk"


# ---------------------------------------------------------------------------
# caret semantics
# ---------------------------------------------------------------------------

def test_caret_bounds_follow_npm():
    assert caret_bounds("^2.1.296") == ((2, 1, 296), (3, 0, 0))
    assert caret_bounds("^0.3.296") == ((0, 3, 296), (0, 4, 0))
    assert caret_bounds("^0.0.3") == ((0, 0, 3), (0, 0, 4))
    assert caret_bounds("1.2.3") == ((1, 2, 3), (1, 2, 4))
    assert caret_bounds(">=1.0.0") is None


def test_satisfies_and_max_satisfying():
    assert satisfies("0.3.300", "^0.3.296")
    assert not satisfies("0.4.0", "^0.3.296")
    assert not satisfies("0.3.295", "^0.3.296")
    assert satisfies("2.9.0", "^2.1.296")
    assert not satisfies("3.0.0", "^2.1.296")
    assert not satisfies("2.2.0-beta.1", "^2.1.296")  # prereleases never adopted
    vs = ["0.3.290", "0.3.296", "0.3.301", "0.4.0", "0.3.302-rc.1"]
    assert max_satisfying(vs, "^0.3.296") == "0.3.301"
    # npm prefers the latest dist-tag when it satisfies the range
    assert max_satisfying(vs, "^0.3.296", latest="0.3.299") == "0.3.299"
    # ...and ignores it when it does not (a new major)
    assert max_satisfying(vs, "^0.3.296", latest="0.4.0") == "0.3.301"
    assert max_satisfying(vs, "^1.0.0") is None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

RANGES = {SDK: "^0.3.296", CC: "^2.1.296", CODEX: "^0.162.1"}


def _make_src(root: Path, ts_body: str = "// v1") -> Path:
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "package.json").write_text(json.dumps(
        {"name": "atn-claude-bridge", "dependencies": dict(RANGES, zod="^4.3.6")}),
        encoding="utf-8")
    (src / "claude-bridge.ts").write_text(ts_body, encoding="utf-8")
    (src / "codex-bridge.ts").write_text("// codex", encoding="utf-8")
    (src / "bun.lock").write_text("stale lockfile", encoding="utf-8")
    return src


class FakeRegistry:
    def __init__(self, latest: dict[str, str]):
        self.latest = dict(latest)
        self.calls = 0

    async def __call__(self, pkg: str) -> dict:
        self.calls += 1
        lo = RANGES[pkg][1:]
        return {"latest": self.latest[pkg], "versions": [lo, self.latest[pkg], "9.9.9"]}


class FakeInstaller:
    """Installs exactly what the staged package.json asks for (exact pins)."""
    def __init__(self):
        self.calls: list[Path] = []
        self.fail = False

    async def __call__(self, d: Path):
        self.calls.append(d)
        assert not (d / "bun.lock").exists(), "a lockfile must never be staged"
        if self.fail:
            return False, "", "boom"
        deps = json.loads((d / "package.json").read_text())["dependencies"]
        for pkg, spec in deps.items():
            ver = spec.lstrip("^")
            pd = d / "node_modules" / pkg
            pd.mkdir(parents=True, exist_ok=True)
            (pd / "package.json").write_text(json.dumps({"version": ver}))
        (d / "node_modules" / SDK / "sdk.mjs").write_text("")
        return True, "bun", ""


class FakeProbe:
    def __init__(self):
        self.calls: list[Path] = []
        self.ok = True

    async def __call__(self, d: Path):
        self.calls.append(d)
        return (True, "ok") if self.ok else (False, "ping failed")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path):
    src = _make_src(tmp_path)
    home = tmp_path / "home" / ".atn" / "bridge"
    reg = FakeRegistry({SDK: "0.3.296", CC: "2.1.296", CODEX: "0.162.1"})
    inst, probe, clock = FakeInstaller(), FakeProbe(), Clock()
    upd = BridgeUpdater(probe=probe, installer=inst, fetch=reg, home=home,
                        src=src, clock=clock)
    return SimpleNamespace(src=src, home=home, reg=reg, inst=inst, probe=probe,
                           clock=clock, upd=upd)


def _current(home: Path) -> dict:
    return json.loads((home / "current.json").read_text())


# ---------------------------------------------------------------------------
# updater behavior
# ---------------------------------------------------------------------------

async def test_no_install_is_left_alone(env):
    res = await env.upd.check(force=True)
    assert res["status"] == "skipped"
    assert env.inst.calls == [] and env.reg.calls == 0


async def test_first_install_pins_exact_versions_and_records_them(env):
    res = await env.upd.check(force=True, install_if_missing=True)
    assert res["status"] == "updated"
    cur = _current(env.home)
    d = env.home / "installs" / cur["install"]
    assert env.upd.current_dir() == d
    assert cur["versions"] == {SDK: "0.3.296", CC: "2.1.296", CODEX: "0.162.1"}
    staged = json.loads((d / "package.json").read_text())["dependencies"]
    assert staged[SDK] == "0.3.296" and staged["zod"] == "^4.3.6"
    assert json.loads((d / "install.json").read_text())["installer"] == "bun"
    assert env.probe.calls == [d]
    st = env.upd.status()
    assert st["mode"] == "managed" and st["versions"][CC] == "2.1.296"
    assert st["last_result"] == "updated" and st["last_check"] == env.clock.t


async def test_daily_gate_then_update_keeps_previous(env):
    await env.upd.check(force=True, install_if_missing=True)
    first = _current(env.home)["install"]

    env.reg.latest[CC] = "2.1.300"
    assert (await env.upd.check())["status"] == "skipped"  # within 24h
    env.clock.t += bu.CHECK_INTERVAL_S + 1
    res = await env.upd.check()
    assert res["status"] == "updated"
    cur = _current(env.home)
    assert cur["previous"] == first and cur["versions"][CC] == "2.1.300"
    assert env.upd.status()["previous_versions"][CC] == "2.1.296"

    # Nothing newer: up_to_date, no install.
    env.clock.t += bu.CHECK_INTERVAL_S + 1
    n = len(env.inst.calls)
    assert (await env.upd.check())["status"] == "up_to_date"
    assert len(env.inst.calls) == n


async def test_probe_failure_keeps_current_and_backs_off(env):
    await env.upd.check(force=True, install_if_missing=True)
    good = _current(env.home)["install"]
    env.reg.latest[SDK] = "0.3.299"
    env.probe.ok = False
    env.clock.t += bu.CHECK_INTERVAL_S + 1
    res = await env.upd.check()
    assert res["status"] == "failed" and "probe" in res["error"]
    assert _current(env.home)["install"] == good
    # the rejected candidate is gone; only the good install remains
    assert [p.name for p in (env.home / "installs").iterdir()] == [good]
    assert env.upd.status()["last_result"] == "failed"
    # a pending rebuild (sources changed) is not retried every hourly tick
    # after a failure; a forced (manual) check is
    (env.src / "claude-bridge.ts").write_text("// v2", encoding="utf-8")
    env.clock.t += 3600
    assert (await env.upd.check())["reason"] == "recent attempt failed"
    env.probe.ok = True
    assert (await env.upd.check(force=True))["status"] == "updated"


async def test_install_failure_keeps_current(env):
    await env.upd.check(force=True, install_if_missing=True)
    good = env.upd.current_dir()
    env.reg.latest[CODEX] = "0.162.5"
    env.inst.fail = True
    res = await env.upd.check(force=True)
    assert res["status"] == "failed" and "install failed" in res["error"]
    assert env.upd.current_dir() == good


async def test_opt_out_skips_registry_but_rebuilds_changed_sources(env):
    await env.upd.check(force=True, install_if_missing=True)
    env.reg.latest[SDK] = "0.3.400"
    calls = env.reg.calls
    env.clock.t += bu.CHECK_INTERVAL_S + 1
    assert (await env.upd.check(allow_registry=False))["status"] == "skipped"
    # A daemon upgrade changes the packaged sources: rebuild, pinned to the
    # installed versions, without consulting the registry.
    (env.src / "claude-bridge.ts").write_text("// v2", encoding="utf-8")
    assert env.upd.needs_rebuild()
    res = await env.upd.check(allow_registry=False)
    assert res["status"] == "updated"
    assert env.reg.calls == calls
    assert res["versions"][SDK] == "0.3.296"
    assert (env.upd.current_dir() / "claude-bridge.ts").read_text() == "// v2"
    assert not env.upd.needs_rebuild()


async def test_gc_spares_installs_serving_a_live_process(env, monkeypatch):
    await env.upd.check(force=True, install_if_missing=True)
    oldest = env.upd.current_dir()

    live = SimpleNamespace(_process=SimpleNamespace(returncode=None),
                           _bridge_script=oldest / "claude-bridge.ts")
    monkeypatch.setattr(bridgemod, "_LIVE_BRIDGES", [live])

    for v in ("2.1.297", "2.1.298"):
        env.reg.latest[CC] = v
        assert (await env.upd.check(force=True))["status"] == "updated"
    cur = _current(env.home)
    names = {p.name for p in (env.home / "installs").iterdir()}
    # current + previous + the one the live process runs from
    assert names == {cur["install"], cur["previous"], oldest.name}

    live._process.returncode = 0  # process exited: next GC reclaims it
    env.upd.gc()
    assert not oldest.exists()


async def test_rollback_swaps_current_and_previous(env):
    await env.upd.check(force=True, install_if_missing=True)
    env.reg.latest[CC] = "2.1.299"
    await env.upd.check(force=True)
    res = await env.upd.rollback()
    assert res["status"] == "rolled_back" and res["versions"][CC] == "2.1.296"
    assert env.upd.status()["versions"][CC] == "2.1.296"


async def test_legacy_flat_install_is_migrated(env):
    # An older daemon's flat ~/.atn/bridge (sources + node_modules in the root)
    env.home.mkdir(parents=True)
    (env.home / "claude-bridge.ts").write_text("// old", encoding="utf-8")
    nm = env.home / "node_modules" / SDK
    nm.mkdir(parents=True)
    (nm / "sdk.mjs").write_text("")
    (nm / "package.json").write_text(json.dumps({"version": "0.3.266"}))
    assert env.upd.status()["mode"] == "legacy"
    assert env.upd.needs_rebuild()  # no/other source stamp
    res = await env.upd.check()  # not forced: rebuild still runs
    assert res["status"] == "updated"
    assert env.upd.status()["mode"] == "managed"
    assert not (env.home / "node_modules").exists()
    assert not (env.home / "claude-bridge.ts").exists()


# ---------------------------------------------------------------------------
# spawn-time resolution in bridge.py
# ---------------------------------------------------------------------------

async def test_spawn_resolution_follows_the_pointer(env, monkeypatch):
    monkeypatch.setattr(bridgemod, "_packaged_bridge_dir", lambda: env.src)
    monkeypatch.setattr(bridgemod, "_runtime_bridge_home", lambda: env.home)
    assert bridgemod.resolve_bridge_dir() == env.home  # nothing installed yet
    assert bridgemod.stage_bridge_sources()[1] is True

    await env.upd.check(force=True, install_if_missing=True)
    first = env.upd.current_dir()
    assert bridgemod.resolve_bridge_dir() == first
    assert bridgemod.stage_bridge_sources() == (first, False)

    resolve = bridgemod._resolve_spawn_script
    assert resolve(Path("ignored"), "claude-bridge.ts", True) == first / "claude-bridge.ts"
    custom = env.src / "claude-bridge.ts"
    assert resolve(custom, "claude-bridge.ts", False) == custom
    # an explicit script into a retired install re-resolves to current
    gone = env.home / "installs" / "retired" / "claude-bridge.ts"
    assert resolve(gone, "claude-bridge.ts", False) == first / "claude-bridge.ts"

    env.reg.latest[CC] = "2.1.297"
    await env.upd.check(force=True)
    assert bridgemod.resolve_bridge_dir() == env.upd.current_dir() != first

    # current broken -> previous is used (automatic rollback)
    import shutil
    shutil.rmtree(env.upd.current_dir() / "node_modules")
    assert bridgemod.resolve_bridge_dir() == first


# ---------------------------------------------------------------------------
# ProviderManager glue
# ---------------------------------------------------------------------------

def test_auto_update_knob_reads_raw_providers_scalar():
    from atn.runtime.provider_manager import ProviderManager

    def pm(raw):
        return SimpleNamespace(_config=SimpleNamespace(raw=raw))
    f = ProviderManager.bridge_auto_update_enabled
    assert f(pm({})) is True
    assert f(pm({"providers": {"claude_max": {}}})) is True
    assert f(pm({"providers": {"bridge_auto_update": False}})) is False
    assert f(pm({"providers": {"bridge_auto_update": "off"}})) is False


def test_scalar_knob_is_not_parsed_as_a_provider(tmp_path):
    from atn.config import load_config
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  bridge_auto_update: false\n  claude_max:\n"
                   "    default_model: sonnet\n", encoding="utf-8")
    c = load_config(cfg)
    assert "bridge_auto_update" not in c.providers
    assert c.raw["providers"]["bridge_auto_update"] is False


async def test_ensure_bridge_deps_builds_when_missing_and_fails_loud(env):
    from atn.runtime.provider_manager import ProviderManager
    pm = object.__new__(ProviderManager)
    pm._config = SimpleNamespace(raw={})
    pm._bridge_updater = env.upd
    pm._bridge_check_task = None

    env.inst.fail = True
    with pytest.raises(ValueError, match="could not be installed"):
        await pm._ensure_bridge_deps()

    env.inst.fail = False
    await pm._ensure_bridge_deps()  # first install, synchronous
    assert env.upd.current_dir() is not None
    assert pm._bridge_check_task is None

    # A working install: connect does not wait; the check runs in background.
    await pm._ensure_bridge_deps()
    assert pm._bridge_check_task is not None
    assert (await pm._bridge_check_task)["status"] == "up_to_date"
    assert pm.bridge_update_status()["versions"][SDK] == "0.3.296"
