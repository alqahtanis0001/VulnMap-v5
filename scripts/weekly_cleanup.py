# scripts/weekly_cleanup.py
# Snapshot each user's available_balance, purge generated ports, then recreate
# one synthetic resolved "ledger port" per user so the computed wallet stays identical.
from __future__ import annotations
import json, os, uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List
from withdrawals_path import get_withdrawals_file

# ---------- Paths ----------
ROOT = Path(__file__).resolve().parents[1]        # project root (contains app.py, data/, templates/, etc.)
DATA_DIR = ROOT / "data"
USERS_FILE = DATA_DIR / "users.json"
WITHDRAWALS_FILE = get_withdrawals_file(DATA_DIR)
GEN_DIR = DATA_DIR / "ports" / "generated_ports"

LEDGER_DIR = DATA_DIR / "ledger"
SNAPSHOT_LATEST = LEDGER_DIR / "wallet_snapshot_latest.json"
CLEANUP_LOG = LEDGER_DIR / "cleanup_log.json"

for p in (GEN_DIR, LEDGER_DIR):
    p.mkdir(parents=True, exist_ok=True)

# ---------- Helpers ----------
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _list_non_admin_usernames() -> List[str]:
    users = _read_json(USERS_FILE, [])
    out = []
    if isinstance(users, list):
        for u in users:
            if not u.get("is_admin"):
                name = (u.get("username") or "").strip().lower()
                if name:
                    out.append(name)
    return sorted(set(out))

def _approved_sum_for(username: str) -> float:
    w = _read_json(WITHDRAWALS_FILE, [])
    total = 0.0
    if isinstance(w, list):
        u = (username or "").lower()
        for item in w:
            try:
                if (item.get("username","").lower() == u) and (item.get("status") == "approved"):
                    total += float(item.get("amount_sar") or 0)
            except Exception:
                pass
    return round(total, 2)

def _scan_resolved_total_for(username: str) -> float:
    """Sum rewards of resolved ports for a given user (pre- or post-cleanup)."""
    total = 0.0
    u = (username or "").lower()
    for p in GEN_DIR.glob("port_*.json"):
        d = _read_json(p, {})
        if not d:
            continue
        try:
            if (d.get("owner","").lower() == u) and d.get("status") == "resolved":
                total += float(d.get("reward") or 0)
        except Exception:
            pass
    return round(total, 2)

def _purge_generated_ports() -> int:
    deleted = 0
    for p in GEN_DIR.glob("*.json"):
        try:
            p.unlink()
            deleted += 1
        except Exception:
            # best-effort
            pass
    return deleted

def _make_ledger_port(username: str, needed_total_earned: float) -> None:
    """
    Create one synthetic resolved port whose reward ensures:
      sum(resolved rewards) == approved_sum + available_pre
    so that available = total_earned - approved_sum remains equal to the snapshot.
    """
    needed_total_earned = round(max(0.0, needed_total_earned), 2)
    now = _utcnow_iso()
    # Stable ID per run; no need to keep across runs because we purge first:
    pid = f"ledger-{uuid.uuid4().hex}"
    port_doc = {
        "id": pid,
        "owner": username,
        "port_number": 65000,           # visible but innocuous
        "reward": needed_total_earned,  # critical: sets total_earned
        "status": "resolved",
        "resolve_delay_sec": 0,
        "created_at": now,
        "discovered_at": now,
        "resolved_at": now,
        "resolve_started_at": None,
        "version": 1,
        "is_ledger": True,
        "note": "Synthetic ledger entry to preserve available_balance across cleanups.",
    }
    out = GEN_DIR / f"port_{pid}.json"
    _write_json_atomic(out, port_doc)

# ---------- Main ----------
def run_weekly_cleanup() -> Dict[str, Any]:
    started = _utcnow_iso()
    # 1) Snapshot pre-cleanup available per user
    snapshot: Dict[str, Dict[str, float]] = {}
    usernames = _list_non_admin_usernames()
    for uname in usernames:
        approved = _approved_sum_for(uname)
        resolved_total = _scan_resolved_total_for(uname)
        available_pre = round(max(0.0, resolved_total - approved), 2)
        snapshot[uname] = {
            "available_pre": available_pre,
            "approved_sum": approved,
            "resolved_total_pre": resolved_total,
        }
    _write_json_atomic(SNAPSHOT_LATEST, {"ts": started, "users": snapshot})
    _write_json_atomic(LEDGER_DIR / f"wallet_{started.replace(':','').replace('-','')}.json", {"ts": started, "users": snapshot})

    # 2) Purge all generated ports
    deleted_files = _purge_generated_ports()

    # 3) Recreate one ledger port per user to preserve availability
    for uname, rec in snapshot.items():
        needed_total_earned = rec["available_pre"] + rec["approved_sum"]
        _make_ledger_port(uname, needed_total_earned)

    # 4) Sanity check after rebuild
    post: Dict[str, Dict[str, float]] = {}
    for uname in usernames:
        approved = _approved_sum_for(uname)
        resolved_total = _scan_resolved_total_for(uname)
        available_post = round(max(0.0, resolved_total - approved), 2)
        post[uname] = {
            "available_post": available_post,
            "approved_sum": approved,
            "resolved_total_post": resolved_total,
        }

    # 5) Log
    log = _read_json(CLEANUP_LOG, [])
    if not isinstance(log, list):
        log = []
    log.append({
        "ts": started,
        "deleted_generated_ports": deleted_files,
        "users": {
            u: {
                **snapshot[u],
                **post.get(u, {})
            } for u in snapshot.keys()
        }
    })
    _write_json_atomic(CLEANUP_LOG, log)

    return {"ok": True, "deleted": deleted_files, "users": list(snapshot.keys())}

if __name__ == "__main__":
    result = run_weekly_cleanup()
    print(json.dumps(result, ensure_ascii=False, indent=2))
