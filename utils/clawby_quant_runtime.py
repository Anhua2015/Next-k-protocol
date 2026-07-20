"""Manage clawby-quant sidecar process (uvicorn) for Next K Protocol embed."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)

_proc: subprocess.Popen | None = None
_log_fp: TextIO[str] | None = None
_lock = threading.Lock()


def vendor_root() -> Path:
    raw = (os.getenv("CLAWBY_QUANT_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / "vendor" / "clawby_quant").resolve()


def clawby_base_url() -> str:
    return (os.getenv("CLAWBY_QUANT_URL") or "http://127.0.0.1:8899").rstrip("/")


def clawby_port() -> int:
    return int(os.getenv("CLAWBY_QUANT_PORT", "8899") or "8899")


def embed_enabled() -> bool:
    # Default ON when unset; set NEXT_K_CLAWBY_EMBED=0 to disable.
    raw = (os.getenv("NEXT_K_CLAWBY_EMBED", "1") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _sidecar_log_path() -> Path:
    data_dir = (os.getenv("DATA_DIR") or "").strip()
    base = Path(data_dir) if data_dir else Path.cwd()
    return (base / "clawby_quant_sidecar.log").resolve()


def _apply_binance_secret_alias(env: dict[str, str]) -> None:
    """Protocol uses BINANCE_API_SECRET; clawby-quant expects BINANCE_SECRET_KEY."""
    if env.get("BINANCE_SECRET_KEY", "").strip():
        return
    secret = (env.get("BINANCE_API_SECRET") or "").strip()
    if secret:
        env["BINANCE_SECRET_KEY"] = secret


def status() -> dict[str, Any]:
    alive = _proc is not None and _proc.poll() is None
    return {
        "ok": True,
        "embed_enabled": embed_enabled(),
        "running": alive,
        "pid": _proc.pid if alive and _proc else None,
        "url": clawby_base_url(),
        "vendor_root": str(vendor_root()),
        "port": clawby_port(),
        "host": "next-k-protocol",
        "log_path": str(_sidecar_log_path()),
    }


def start_sidecar() -> dict[str, Any]:
    """Start uvicorn backend.main:app in vendor root (no-op if already running)."""
    global _proc, _log_fp
    if not embed_enabled():
        return {**status(), "started": False, "reason": "embed_disabled"}

    root = vendor_root()
    if not (root / "backend" / "main.py").is_file():
        logger.error("clawby-quant vendor missing at %s", root)
        return {**status(), "started": False, "reason": "vendor_missing", "root": str(root)}

    with _lock:
        if _proc is not None and _proc.poll() is None:
            return {**status(), "started": False, "reason": "already_running"}

        port = clawby_port()
        env = os.environ.copy()
        env.setdefault("CLAWBY_API_PREFIX", "/api/clawby-quant")
        _apply_binance_secret_alias(env)
        data_dir = (os.getenv("DATA_DIR") or "").strip()
        if data_dir:
            env.setdefault("QB_DB_PATH", str(Path(data_dir) / "clawby_quantbot.db"))
            # Trade journal JSONL must survive redeploys (not under vendor/).
            env.setdefault("QB_JOURNAL_DIR", str(Path(data_dir) / "clawby_quant_journal"))

        log_path = _sidecar_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if _log_fp is not None:
                try:
                    _log_fp.close()
                except Exception:
                    pass
            _log_fp = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
            _log_fp.write(f"\n--- start sidecar port={port} ---\n")
            _log_fp.flush()
        except Exception as e:
            logger.warning("clawby sidecar log open failed (%s); using DEVNULL", e)
            _log_fp = None

        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ]
        logger.info("Starting clawby-quant sidecar: %s (cwd=%s)", " ".join(cmd), root)
        out = _log_fp if _log_fp is not None else subprocess.DEVNULL
        try:
            _proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            logger.exception("clawby-quant start failed: %s", e)
            return {**status(), "started": False, "reason": str(e)}

        time.sleep(1.2)
        alive = _proc.poll() is None
        if not alive:
            logger.error(
                "clawby-quant exited immediately; see %s",
                log_path,
            )
            return {**status(), "started": False, "reason": "exited_immediately"}
        return {**status(), "started": True}


def stop_sidecar() -> None:
    global _proc, _log_fp
    with _lock:
        if _proc is not None and _proc.poll() is None:
            logger.info("Stopping clawby-quant sidecar pid=%s", _proc.pid)
            try:
                _proc.terminate()
                _proc.wait(timeout=8)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
        _proc = None
        if _log_fp is not None:
            try:
                _log_fp.close()
            except Exception:
                pass
            _log_fp = None
