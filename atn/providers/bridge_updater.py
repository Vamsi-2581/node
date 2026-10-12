"""Keeps the subscription bridges' npm dependencies current, independent of
the daemon release.

The bridges (claude-bridge.ts, codex-bridge.ts) run on three fast-moving npm
packages (``TRACKED_PACKAGES``). Their ranges in bridge/package.json are
caret ranges; this module resolves the newest version each range allows and
keeps ``~/.atn/bridge`` on it:

* **When**: at most once per 24h (the daemon's background loop), when the
  packaged bridge sources change (a daemon upgrade), on provider configure,
  and on a manual trigger. Opt out with ``providers.bridge_auto_update:
  false`` in config.yaml (source-change rebuilds still run, pinned to the
  versions already installed).
* **Safety**: every update installs into a FRESH dir
  (``~/.atn/bridge/installs/<id>``), runs the bridge ping probe(s) against
  it, and only then flips ``current.json`` (atomic ``os.replace``). The old
  install stays as ``previous`` for rollback. Running bridge processes keep
  the install they were spawned from: the swap reaches the next spawn
  (``bridge.resolve_bridge_dir`` is read per spawn), and an install a live
  process uses is never deleted.
* **bun and npm alike**: the tracked packages are pinned to the exact
  resolved versions in the staged package.json and no lockfile is shipped,
  so either installer lands the same versions.
* **Observable**: resolved versions are recorded in ``install.json`` and
  ``current.json``; check outcomes in ``update_state.json``; both are
  surfaced through ``status()`` (provider_list ``bridge_runtime``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .bridge import (
    _BRIDGE_SOURCE_GLOBS,
    _CURRENT_FILE,
    _INSTALLS_DIRNAME,
    _install_usable,
    _node_modules_ready,
    _packaged_bridge_dir,
    _runtime_bridge_home,
    _source_stamp,
    bridge_dirs_in_use,
    is_dev_bridge_dir,
    read_current_pointer,
)

log = logging.getLogger(__name__)

TRACKED_PACKAGES = (
    "@anthropic-ai/claude-agent-sdk",
    "@anthropic-ai/claude-code",
    "@openai/codex-sdk",
)
CHECK_INTERVAL_S = 24 * 3600
INSTALL_TIMEOUT_S = 600
_STATE_FILE = "update_state.json"
_LOCK_FILE = "update.lock"
_LOCK_STALE_S = 30 * 60
# A failed build (install or probe) is not retried by the background loop
# for this long; a manual trigger or provider configure (force) still is.
_RETRY_BACKOFF_S = 6 * 3600

# probe(dir) -> (ok, detail); fetch(pkg) -> {"latest": str, "versions": [..]};
# installer(dir) -> (ok, tool, error).
ProbeFn = Callable[[Path], Awaitable[tuple[bool, str]]]
FetchFn = Callable[[str], Awaitable[dict[str, Any]]]
InstallFn = Callable[[Path], Awaitable[tuple[bool, str, str]]]


# ---------------------------------------------------------------------------
# Caret-range semver (the subset package.json uses: ^X.Y.Z / exact X.Y.Z)
# ---------------------------------------------------------------------------

_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(v: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) for a release version; None for prereleases or
    anything unparseable (never auto-adopted)."""
    m = _VER_RE.match(str(v).strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def caret_bounds(spec: str) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """[lo, hi) for ``^X.Y.Z`` (npm caret semantics: the left-most non-zero
    component is fixed) or an exact ``X.Y.Z``. None if unsupported."""
    spec = str(spec).strip()
    if spec.startswith("^"):
        lo = parse_version(spec[1:])
        if lo is None:
            return None
        major, minor, patch = lo
        if major > 0:
            hi = (major + 1, 0, 0)
        elif minor > 0:
            hi = (0, minor + 1, 0)
        else:
            hi = (0, 0, patch + 1)
        return lo, hi
    exact = parse_version(spec)
    if exact is None:
        return None
    return exact, (exact[0], exact[1], exact[2] + 1)


def satisfies(version: str, spec: str) -> bool:
    v = parse_version(version)
    bounds = caret_bounds(spec)
    return v is not None and bounds is not None and bounds[0] <= v < bounds[1]


def max_satisfying(versions: list[str], spec: str, latest: str = "") -> str | None:
    """The version npm would pick for ``spec``: the ``latest`` dist-tag when
    it satisfies the range, else the highest release version that does."""
    if latest and satisfies(latest, spec):
        return latest
    ok = [v for v in versions if satisfies(v, spec)]
    return max(ok, key=parse_version) if ok else None


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_package_ranges(pkg_json: Path) -> dict[str, str]:
    deps = _read_json(pkg_json).get("dependencies") or {}
    return {p: str(deps[p]) for p in TRACKED_PACKAGES if p in deps}


def installed_versions(bridge_dir: Path) -> dict[str, str]:
    """Versions actually present under ``bridge_dir/node_modules``."""
    out: dict[str, str] = {}
    for pkg in TRACKED_PACKAGES:
        v = _read_json(bridge_dir / "node_modules" / pkg / "package.json").get("version")
        if v:
            out[pkg] = str(v)
    return out


# ---------------------------------------------------------------------------
# Default network / installer implementations
# ---------------------------------------------------------------------------

async def fetch_npm_versions(pkg: str) -> dict[str, Any]:
    """``{"latest", "versions"}`` from the npm registry's abbreviated
    packument (honors ``npm_config_registry``)."""
    import httpx
    registry = (os.environ.get("npm_config_registry")
                or os.environ.get("NPM_CONFIG_REGISTRY")
                or "https://registry.npmjs.org").rstrip("/")
    url = f"{registry}/{pkg.replace('/', '%2f')}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(
            url, headers={"Accept": "application/vnd.npm.install-v1+json"})
        resp.raise_for_status()
        data = resp.json()
    return {
        "latest": str((data.get("dist-tags") or {}).get("latest") or ""),
        "versions": list((data.get("versions") or {}).keys()),
    }


async def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        import subprocess
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return -1, f"timed out after {int(timeout)}s"
    text = (err or b"").decode(errors="replace") or (out or b"").decode(errors="replace")
    return proc.returncode or 0, text[-400:]


def make_installer(
    find_bun: Callable[[], Awaitable[str | None]] | None = None,
    timeout: float = INSTALL_TIMEOUT_S,
) -> InstallFn:
    """bun first (auto-installed via ``find_bun`` when absent), npm fallback.
    Both run with no lockfile against the pinned package.json, so they land
    the same tracked versions."""
    async def install(bridge_dir: Path) -> tuple[bool, str, str]:
        bun = shutil.which("bun")
        if not bun and find_bun is not None:
            bun = await find_bun()
        errors: list[str] = []
        if bun:
            code, text = await _run([bun, "install"], bridge_dir, timeout)
            if code == 0 and _node_modules_ready(bridge_dir):
                return True, "bun", ""
            errors.append(f"bun install: {text or f'exit {code}'}")
            shutil.rmtree(bridge_dir / "node_modules", ignore_errors=True)
        npm = shutil.which("npm")
        if npm:
            code, text = await _run(
                [npm, "install", "--no-audit", "--no-fund"], bridge_dir, timeout)
            if code == 0 and _node_modules_ready(bridge_dir):
                return True, "npm", ""
            errors.append(f"npm install: {text or f'exit {code}'}")
        if not bun and not npm:
            return False, "", ("neither 'bun' nor 'npm' was found (and bun "
                               "auto-install failed); install bun from https://bun.sh")
        return False, "", "; ".join(errors)
    return install


# ---------------------------------------------------------------------------
# The updater
# ---------------------------------------------------------------------------

class BridgeUpdater:
    """Builds, probes and swaps managed bridge installs under ``home``."""

    def __init__(
        self,
        *,
        probe: ProbeFn,
        installer: InstallFn,
        fetch: FetchFn = fetch_npm_versions,
        home: Path | None = None,
        src: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._probe = probe
        self._installer = installer
        self._fetch = fetch
        self.home = home or _runtime_bridge_home()
        self.src = src or _packaged_bridge_dir()
        self._clock = clock
        self._lock = asyncio.Lock()

    # -- state -------------------------------------------------------------

    @property
    def installs_dir(self) -> Path:
        return self.home / _INSTALLS_DIRNAME

    def state(self) -> dict[str, Any]:
        return _read_json(self.home / _STATE_FILE)

    def _save_state(self, **fields: Any) -> None:
        st = self.state()
        st.update(fields)
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(self.home / _STATE_FILE, st)
        except OSError as exc:
            log.warning("Could not record bridge update state: %s", exc)

    def is_dev(self) -> bool:
        return self.src == _packaged_bridge_dir() and is_dev_bridge_dir()

    def current_dir(self) -> Path | None:
        """The dir new bridge processes spawn from (None: nothing installed)."""
        if self.is_dev():
            return self.src
        ptr = read_current_pointer(self.home)
        for key in ("install", "previous"):
            name = ptr.get(key)
            if isinstance(name, str) and name and _install_usable(self.installs_dir / name):
                return self.installs_dir / name
        if _install_usable(self.home):  # legacy flat layout of older daemons
            return self.home
        return None

    def needs_rebuild(self) -> bool:
        """No usable install, or it was built from other packaged sources."""
        cur = self.current_dir()
        if cur is None:
            return True
        if cur == self.src:
            return False
        try:
            have = (cur / ".source_stamp").read_text(encoding="utf-8").strip()
        except OSError:
            have = ""
        return have != _source_stamp(self.src)

    def status(self, auto_update: bool = True) -> dict[str, Any]:
        cur = self.current_dir()
        st = self.state()
        ptr = read_current_pointer(self.home)
        prev = ptr.get("previous")
        prev_dir = self.installs_dir / prev if isinstance(prev, str) and prev else None
        return {
            "auto_update": bool(auto_update),
            "mode": ("dev" if cur is not None and cur == self.src
                     else "managed" if cur is not None and cur != self.home
                     else "legacy" if cur is not None else "not_installed"),
            "install_dir": str(cur) if cur else "",
            "versions": installed_versions(cur) if cur else {},
            "installer": ptr.get("installer", ""),
            "installed_at": ptr.get("installed_at"),
            "previous_versions": (installed_versions(prev_dir)
                                  if prev_dir and _install_usable(prev_dir) else {}),
            "last_check": st.get("last_check"),
            "last_result": st.get("last_result", ""),
            "last_error": st.get("last_error", ""),
            "available": st.get("available", {}),
        }

    # -- cross-process lock --------------------------------------------------

    def _acquire_file_lock(self) -> bool:
        self.home.mkdir(parents=True, exist_ok=True)
        lock = self.home / _LOCK_FILE
        try:
            if self._clock() - lock.stat().st_mtime > _LOCK_STALE_S:
                lock.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True

    def _release_file_lock(self) -> None:
        try:
            (self.home / _LOCK_FILE).unlink()
        except OSError:
            pass

    # -- resolution ----------------------------------------------------------

    async def resolve_latest(self) -> dict[str, str]:
        """Newest version each tracked caret range allows (registry lookup)."""
        ranges = read_package_ranges(self.src / "package.json")
        out: dict[str, str] = {}
        for pkg, spec in ranges.items():
            info = await self._fetch(pkg)
            best = max_satisfying(list(info.get("versions") or []), spec,
                                  str(info.get("latest") or ""))
            if best:
                out[pkg] = best
        return out

    def _recorded_pins(self) -> dict[str, str]:
        """Installed versions that still satisfy the packaged ranges (used
        when the registry is not consulted: opt-out, offline)."""
        cur = self.current_dir()
        if cur is None or cur == self.src:
            return {}
        ranges = read_package_ranges(self.src / "package.json")
        have = installed_versions(cur)
        return {p: v for p, v in have.items() if p in ranges and satisfies(v, ranges[p])}

    # -- the public operations ----------------------------------------------

    async def check(
        self,
        *,
        force: bool = False,
        allow_registry: bool = True,
        install_if_missing: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        """Check for newer in-range versions (and stale sources) and update.

        ``force`` skips the 24h gate. ``install_if_missing`` builds a first
        install when none exists (provider configure); otherwise a machine
        that never set up a bridge is left alone. Never raises for update
        failures while a working install exists; returns a result dict with
        ``status`` in up_to_date / updated / failed / skipped / dev.
        """
        async with self._lock:
            return await self._check_locked(
                force=force, allow_registry=allow_registry,
                install_if_missing=install_if_missing, reason=reason)

    async def _check_locked(
        self, *, force: bool, allow_registry: bool,
        install_if_missing: bool, reason: str,
    ) -> dict[str, Any]:
        if self.is_dev():
            return {"status": "dev", "versions": installed_versions(self.src)}
        cur = self.current_dir()
        if cur is None and not install_if_missing:
            return {"status": "skipped", "reason": "no bridge installed"}

        rebuild = self.needs_rebuild()
        now = self._clock()
        last = self.state().get("last_check") or 0
        due = allow_registry and (force or now - float(last) >= CHECK_INTERVAL_S)
        if not rebuild and not due:
            return {"status": "skipped", "reason": "checked recently"}
        st0 = self.state()
        if (not force and st0.get("last_result") == "failed"
                and now - float(st0.get("last_attempt") or 0) < _RETRY_BACKOFF_S):
            return {"status": "skipped", "reason": "recent attempt failed",
                    "error": st0.get("last_error", "")}

        pins: dict[str, str] = {}
        if due:
            try:
                pins = await self.resolve_latest()
            except Exception as exc:  # noqa: BLE001 — offline is not fatal
                log.warning("Bridge update check failed (registry): %s", exc)
                self._save_state(last_check=now, last_result="check_failed",
                                 last_error=f"registry: {exc}")
                if not rebuild:
                    return {"status": "failed", "error": str(exc)}
                pins = self._recorded_pins()
            else:
                self._save_state(available=pins)
        else:
            pins = self._recorded_pins()

        have = installed_versions(cur) if cur is not None else {}
        newer = {p: v for p, v in pins.items() if have.get(p) != v}
        if not rebuild and not newer:
            log.info("Bridge deps up to date: %s", _fmt(have))
            self._save_state(last_check=now, last_result="up_to_date", last_error="")
            return {"status": "up_to_date", "versions": have}

        log.info("Bridge update (%s): %s -> %s", reason or "check",
                 _fmt(have) or "none", _fmt(pins) or "package.json ranges")
        result = await self._build_and_swap(pins)
        st: dict[str, Any] = {"last_attempt": now, "last_result": result["status"],
                              "last_error": result.get("error", "")}
        if due:
            st["last_check"] = now
        self._save_state(**st)
        return result

    async def rollback(self) -> dict[str, Any]:
        """Point ``current`` back at ``previous`` (they swap roles)."""
        async with self._lock:
            ptr = read_current_pointer(self.home)
            prev = ptr.get("previous")
            if not (isinstance(prev, str) and prev
                    and _install_usable(self.installs_dir / prev)):
                return {"status": "failed", "error": "no previous install to roll back to"}
            new_ptr = dict(ptr, install=prev, previous=ptr.get("install"),
                           installed_at=self._clock(),
                           versions=installed_versions(self.installs_dir / prev))
            _write_json_atomic(self.home / _CURRENT_FILE, new_ptr)
            log.info("Bridge rolled back to install %s: %s", prev, _fmt(new_ptr["versions"]))
            self._save_state(last_result="rolled_back", last_error="")
            return {"status": "rolled_back", "versions": new_ptr["versions"]}

    # -- build / probe / swap -------------------------------------------------

    async def _build_and_swap(self, pins: dict[str, str]) -> dict[str, Any]:
        if not self._acquire_file_lock():
            return {"status": "skipped", "reason": "another update is in progress"}
        try:
            return await self._build_and_swap_unlocked(pins)
        finally:
            self._release_file_lock()

    async def _build_and_swap_unlocked(self, pins: dict[str, str]) -> dict[str, Any]:
        install_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self._clock())) \
            + "-" + secrets.token_hex(3)
        new_dir = self.installs_dir / install_id
        try:
            new_dir.mkdir(parents=True)
            for pat in _BRIDGE_SOURCE_GLOBS:
                for f in self.src.glob(pat):
                    shutil.copy2(f, new_dir / f.name)
            pkg_path = new_dir / "package.json"
            pkg = _read_json(pkg_path)
            deps = pkg.setdefault("dependencies", {})
            for name, ver in pins.items():
                deps[name] = ver  # exact pin: bun and npm resolve identically
            pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
        except OSError as exc:
            shutil.rmtree(new_dir, ignore_errors=True)
            return {"status": "failed", "error": f"staging failed: {exc}"}

        ok, tool, err = await self._installer(new_dir)
        if not ok:
            log.warning("Bridge install into %s failed: %s", new_dir, err)
            shutil.rmtree(new_dir, ignore_errors=True)
            return {"status": "failed", "error": f"install failed: {err}"}

        got = installed_versions(new_dir)
        wrong = {p: got.get(p) for p, v in pins.items() if got.get(p) != v}
        if wrong:
            shutil.rmtree(new_dir, ignore_errors=True)
            return {"status": "failed",
                    "error": f"installer ({tool}) resolved unexpected versions: {wrong}"}

        probe_ok, detail = await self._probe(new_dir)
        if not probe_ok:
            log.warning("Bridge install %s failed its probe (%s); keeping the "
                        "current install", _fmt(got), detail)
            shutil.rmtree(new_dir, ignore_errors=True)
            return {"status": "failed", "error": f"probe failed: {detail}",
                    "candidate": got}

        (new_dir / ".source_stamp").write_text(_source_stamp(self.src), encoding="utf-8")
        built_at = self._clock()
        _write_json_atomic(new_dir / "install.json", {
            "versions": got, "installer": tool, "built_at": built_at, "pins": pins,
        })
        old = read_current_pointer(self.home)
        old_install = old.get("install")
        prev = old_install if isinstance(old_install, str) and old_install \
            and _install_usable(self.installs_dir / old_install) else None
        _write_json_atomic(self.home / _CURRENT_FILE, {
            "install": install_id, "previous": prev, "versions": got,
            "installer": tool, "installed_at": built_at,
        })
        log.info("Bridge deps now %s (installed with %s, install %s); running "
                 "bridge processes switch on their next spawn",
                 _fmt(got), tool, install_id)
        self.gc()
        return {"status": "updated", "versions": got, "installer": tool,
                "install": install_id}

    def gc(self) -> None:
        """Delete installs that are neither current, previous, nor serving a
        live bridge process. The legacy flat layout's node_modules goes once
        a managed install is current and no bridge process is running."""
        ptr = read_current_pointer(self.home)
        keep = {ptr.get("install"), ptr.get("previous")}
        in_use = bridge_dirs_in_use()
        try:
            entries = list(self.installs_dir.iterdir())
        except OSError:
            entries = []
        for d in entries:
            if d.name in keep or not d.is_dir():
                continue
            try:
                if d.resolve() in in_use:
                    continue
            except OSError:
                continue
            shutil.rmtree(d, ignore_errors=True)
        if ptr.get("install") and not in_use and (self.home / "node_modules").is_dir():
            shutil.rmtree(self.home / "node_modules", ignore_errors=True)
            legacy = [self.home / n for n in (".source_stamp", "bun.lock", "package-lock.json")]
            for pat in _BRIDGE_SOURCE_GLOBS:
                legacy.extend(self.home.glob(pat))
            for f in legacy:
                try:
                    f.unlink()
                except OSError:
                    pass


def _fmt(versions: dict[str, str]) -> str:
    return ", ".join(f"{p.split('/')[-1]}@{v}" for p, v in sorted(versions.items()))
