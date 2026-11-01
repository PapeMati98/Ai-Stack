"""Windows Telegram/Desktop controller entry.

Usage:
  python project/runtime/run.py --config PATH --once
  python project/runtime/run.py --config PATH --serve
  python project/runtime/run.py --config PATH --status

Token from process env TELEGRAM_BOT_TOKEN only. No argv secret, no webhook,
no public listener, no shell concatenation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "transport", ROOT / "automation", ROOT / "native"):
    text = str(extra)
    if text not in sys.path:
        sys.path.insert(0, text)


def _emit(doc: dict) -> int:
    json.dump(doc, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _err(reason: str) -> int:
    _emit(
        {"schema": "1.0", "kind": "error", "payload": {"reason": reason}}
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openclaw-runtime")
    p.add_argument("--config", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--status", action="store_true")
    return p


def _open_store(cfg):
    from openclaw_control.store import ControlStore
    from openclaw_ops.ledger import Ledger

    if not cfg.authority_db:
        raise ValueError("missing-authority-db")
    ledger = Ledger(cfg.authority_db)
    store = ControlStore(cfg.authority_db)
    return ledger, store


def _registry(cfg, store, clock=None, injected=None):
    from runtime.registry import AdapterRegistry
    from openclaw_control.adapters import DisabledAdapter
    from runtime.claude_desktop import ClaudeDesktopAdapter
    from runtime.codex_adapter import CodexControlAdapter
    from codex_desktop import (
        CAPABILITIES,
        AppTools, CodexDesktop,
        pipe_available,
        process_available,
    )

    registry = AdapterRegistry()
    if injected is not None:
        return injected
    if cfg.claude_enabled:
        registry.register(
            "claude-desktop",
            ClaudeDesktopAdapter(),
            verified=True,
            capabilities={"cli_fallback": False},
        )
    else:
        registry.register(
            "claude-desktop",
            DisabledAdapter("claude-desktop"),
            verified=False,
            reason="claude-unverified",
            capabilities={"cli_fallback": False},
        )
    spec = cfg.codex
    if not cfg.enabled or not spec.enabled:
        registry.register(
            "codex-desktop",
            DisabledAdapter("codex-desktop"),
            verified=False,
            reason="codex-disabled-in-config",
            capabilities=CAPABILITIES,
        )
        return registry
    pipe, pid = spec.pipe_path, spec.pid
    candidates = [(pipe, pid)]
    if spec.server_image:
        try:
            from pipe_discovery import discover_pipe
            candidates = discover_pipe(spec.server_image, preferred=spec.pipe_path, all_matches=True)
            pipe, pid = candidates[0]
        except OSError:
            pipe, pid = '', 0
    if not pipe or (pid and not process_available(pid)):
        registry.register(
            "codex-desktop",
            DisabledAdapter("codex-desktop"),
            verified=True,
            reason="pid-unavailable",
            capabilities=CAPABILITIES,
        )
        return registry
    if not pipe_available(pipe):
        registry.register(
            "codex-desktop",
            DisabledAdapter("codex-desktop"),
            verified=True,
            reason="pipe-unavailable",
            capabilities=CAPABILITIES,
        )
        return registry
    allowed = {t.native_thread: {'rollout_path': t.rollout_path}
                   for t in cfg.targets if t.provider == 'codex-desktop'
                   and t.enabled and t.verified and t.native_thread}
    for candidate_pipe, candidate_pid in candidates:
        app = None
        try:
            if not allowed or any(not value['rollout_path'] for value in allowed.values()):
                raise ValueError('No verified rollout binding')
            app = AppTools(spec.node_path, spec.plugin_path, spec.plugin_sha256,
                           spec.caller_thread, candidate_pipe)
            desktop = CodexDesktop(app, allowed, Path(spec.sessions_root))
            # Other app pipes can belong to browser-only channels. Probe read-only.
            desktop.status(next(iter(allowed)))
            adapter = CodexControlAdapter(desktop, pipe_path=candidate_pipe, pid=candidate_pid, clock=clock)
            registry.register('codex-desktop', adapter, verified=True, capabilities=CAPABILITIES)
            return registry
        except Exception:
            if app is not None:
                app.close()
    registry.register('codex-desktop', DisabledAdapter('codex-desktop'),
                      verified=True, reason='native-session-unavailable', capabilities=CAPABILITIES)
    return registry


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    from runtime.config import ConfigError, load_config
    from runtime.controller import Controller
    from runtime.instance import InstanceLock
    from openclaw_control.botapi import urllib_no_redirect_opener

    try:
        cfg = load_config(args.config)
    except (OSError, ConfigError, ValueError, json.JSONDecodeError):
        return _err("bad-config")

    try:
        ledger, store = _open_store(cfg)
    except Exception:
        return _err("authority-db")

    lock_path = cfg.authority_db + ".runtime.lock"
    lock = InstanceLock(lock_path)
    try:
        registry = _registry(cfg, store)
        controller = Controller(
            cfg,
            store,
            os.environ,
            urllib_no_redirect_opener,
            registry,
            ledger=ledger,
        )
        controller.registry_factory = lambda: _registry(cfg, store)
        if args.status:
            return _emit(controller.status_doc())
        if not lock.acquire():
            return _err("already-running")
        if args.once:
            return _emit(controller.once())
        # --serve is quiet: no raw results, secrets, or prompts.
        controller.serve_forever()
        return 0
    finally:
        if 'controller' in locals():
            controller.registry.close()
        lock.release()
        try:
            store.close()
        except Exception:
            pass
        try:
            ledger.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
