"""Helpers to persist and override Rayan's wallet across restarts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from remote_wallet_store import (
    fetch_remote_wallet,
    persist_remote_wallet,
    has_remote_wallet_store,
)

RAYAN_USERNAME = "rayan"
EPSILON = 0.01


def is_rayan(username: str) -> bool:
    return (username or "").strip().lower() == RAYAN_USERNAME


def get_rayan_wallet_file(data_dir: Path) -> Path:
    return Path(data_dir) / "wallet_rayan.json"


def _read_json(path: Path, default):
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_atomic(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _sanitize_wallet(wallet: Dict[str, float]) -> Dict[str, float]:
    return {
        "available_balance": round(float(wallet.get("available_balance", 0.0)), 2),
        "total_earned": round(float(wallet.get("total_earned", 0.0)), 2),
    }


def _merge_wallets(*wallets: Optional[Dict[str, float]]) -> Dict[str, float]:
    best_total = 0.0
    best_available = 0.0
    for wallet in wallets:
        if not wallet:
            continue
        sanitized = _sanitize_wallet(wallet)
        best_total = max(best_total, sanitized["total_earned"])
        best_available = max(best_available, sanitized["available_balance"])
    best_available = min(best_available, best_total)
    return {
        "total_earned": round(best_total, 2),
        "available_balance": round(best_available, 2),
    }


def persist_rayan_wallet(
    data_dir: Path, wallet: Dict[str, float], *, force_remote: bool = False
) -> Dict[str, float]:
    sanitized = _sanitize_wallet(wallet)
    fp = get_rayan_wallet_file(data_dir)
    existing = _read_json(fp, {})
    changed = existing != sanitized
    if changed:
        _write_json_atomic(fp, sanitized)
    if has_remote_wallet_store() and (changed or force_remote):
        try:
            persist_remote_wallet(sanitized)
        except Exception:
            pass
    return sanitized


def load_rayan_wallet(data_dir: Path, computed_wallet: Dict[str, float]) -> Dict[str, float]:
    """
    Persist the latest computed wallet so that new earnings/withdrawals are
    reflected immediately while still keeping a durable snapshot on disk.
    """
    computed = _sanitize_wallet(computed_wallet)
    fp = get_rayan_wallet_file(data_dir)
    persisted = _read_json(fp, None)
    if isinstance(persisted, dict):
        persisted = _sanitize_wallet(persisted)
    else:
        persisted = None

    remote = None
    if has_remote_wallet_store():
        remote = fetch_remote_wallet()
        if isinstance(remote, dict):
            remote = _sanitize_wallet(remote)
        else:
            remote = None

    merged = _merge_wallets(computed, persisted, remote)

    force_remote = False
    if remote is None:
        force_remote = True
    else:
        if remote["total_earned"] + EPSILON < merged["total_earned"]:
            force_remote = True
        if remote["available_balance"] + EPSILON < merged["available_balance"]:
            force_remote = True

    return persist_rayan_wallet(data_dir, merged, force_remote=force_remote)


def reset_rayan_wallet(data_dir: Path, total_earned: float = 0.0) -> Dict[str, float]:
    """Force Rayan's wallet to a known baseline (e.g., after admin reset)."""
    return persist_rayan_wallet(
        data_dir,
        {"available_balance": 0.0, "total_earned": round(float(total_earned or 0.0), 2)},
        force_remote=True,
    )
