# port_logic.py
# Shared business logic for per-port JSON files:
# - Atomic writes
# - Lockfiles for single-writer per port
# - Idempotency for POSTs
# - Read helpers for dashboards and admin stats

from __future__ import annotations
import time

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from withdrawals_path import get_withdrawals_file
from rayan_wallet import is_rayan, load_rayan_wallet

# ---------- Paths ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PORTS_DIR = DATA_DIR / "ports" / "generated_ports"
LOCKS_DIR = DATA_DIR / "ports" / "locks"
PROCESSED_FILE = DATA_DIR / "ports" / "processed_requests.json"
WITHDRAWALS_FILE = get_withdrawals_file(DATA_DIR)
USERS_FILE = DATA_DIR / "users.json"

# ---------- Utilities ----------

# --- Utilities (keep this above any function that calls it) ---
def _ensure_dirs():
    PORTS_DIR.mkdir(parents=True, exist_ok=True)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    if not PROCESSED_FILE.exists():
        _write_json_atomic(PROCESSED_FILE, {"keys": {}})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json_atomic(path: Path, data) -> None:
    """
    Windows-safe atomic write with retries.
    Handles both PermissionError (WinError 32) and FileNotFoundError (AV/indexers deleting temp).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    def write_tmp():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

    # We may need to recreate the .tmp if it vanished between attempts
    for i in range(10):  # ~2.75s worst-case
        # ensure temp exists for this attempt
        if not tmp.exists():
            write_tmp()
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # File briefly locked by another process
            time.sleep(0.05 * (i + 1))
            continue
        except FileNotFoundError:
            # Temp vanished; loop will rewrite and retry
            time.sleep(0.05 * (i + 1))
            continue

    # Final guarded attempt
    if not tmp.exists():
        write_tmp()
    os.replace(tmp, path)


# ---------- Data structures ----------
VALID_STATUSES = {"assigned", "discovered", "resolved", "archived"}

@dataclass
class Port:
    id: str
    owner: str
    port_number: int
    reward: float
    status: str
    resolve_delay_sec: int
    created_at: str
    discovered_at: Optional[str] = None
    resolved_at: Optional[str] = None
    # Starts when the user first clicks "حل"
    resolve_started_at: Optional[str] = None
    version: int = 1

    @staticmethod
    def file_for(pid: str) -> Path:
        return PORTS_DIR / f"port_{pid}.json"

    @staticmethod
    def from_dict(d: dict) -> "Port":
        return Port(
            id=d["id"],
            owner=d["owner"],
            port_number=int(d["port_number"]),
            reward=float(d["reward"]),
            status=d["status"],
            resolve_delay_sec=int(d.get("resolve_delay_sec", 0)),
            created_at=d.get("created_at") or _utcnow_iso(),
            discovered_at=d.get("discovered_at"),
            resolved_at=d.get("resolved_at"),
            resolve_started_at=d.get("resolve_started_at"),
            version=int(d.get("version", 1)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner": self.owner,
            "port_number": self.port_number,
            "reward": round(self.reward, 2),
            "status": self.status,
            "resolve_delay_sec": self.resolve_delay_sec,
            "created_at": self.created_at,
            "discovered_at": self.discovered_at,
            "resolved_at": self.resolved_at,
            "resolve_started_at": self.resolve_started_at,
            "version": self.version,
        }

# ---------- Lockfiles ----------
def _lock_path(pid: str) -> Path:
    return LOCKS_DIR / f"{pid}.lock"

def acquire_lock(pid: str, ttl_seconds: int = 30) -> bool:
    """Create a lockfile atomically. If stale (> ttl), remove and take it."""
    _ensure_dirs()
    p = _lock_path(pid)
    if p.exists():
        try:
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            if age > timedelta(seconds=ttl_seconds):
                p.unlink(missing_ok=True)  # stale lock
            else:
                return False
        except Exception:
            return False

    # atomic create (O_EXCL) to avoid races (Windows-safe)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(p), flags)
        try:
            os.write(fd, _utcnow_iso().encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False

def release_lock(pid: str) -> None:
    try:
        _lock_path(pid).unlink(missing_ok=True)
    except Exception:
        pass

# ---------- Idempotency ----------
def idempotency_seen(key: str) -> bool:
    data = _read_json(PROCESSED_FILE, {"keys": {}})
    return key in data.get("keys", {})

def record_idempotency(key: str, result: dict) -> None:
    data = _read_json(PROCESSED_FILE, {"keys": {}})
    data.setdefault("keys", {})[key] = {
        "result": result,
        "ts": _utcnow_iso()
    }
    _write_json_atomic(PROCESSED_FILE, data)

# ---------- CRUD helpers ----------
def create_port(owner: str, port_number: int, reward: float, resolve_delay_sec: int = 0) -> Port:
    _ensure_dirs()
    pid = str(uuid.uuid4())
    port = Port(
        id=pid,
        owner=owner.lower(),
        port_number=int(port_number),
        reward=float(reward),
        status="assigned",
        resolve_delay_sec=int(resolve_delay_sec),
        created_at=_utcnow_iso(),
        version=1,
    )
    _write_json_atomic(Port.file_for(pid), port.to_dict())
    return port

def load_port(pid: str) -> Optional[Port]:
    path = Port.file_for(pid)
    if not path.exists():
        return None
    data = _read_json(path, {})
    try:
        return Port.from_dict(data)
    except Exception:
        return None

def save_port(port: Port) -> None:
    """Atomic write; increments version."""
    port.version = int(port.version or 1) + 1
    _write_json_atomic(Port.file_for(port.id), port.to_dict())

def list_ports_for_user(username: str) -> List[Port]:
    username = (username or "").lower()
    _ensure_dirs()
    ports: List[Port] = []
    for p in PORTS_DIR.glob("port_*.json"):
        d = _read_json(p, {})
        if not d or (d.get("owner", "").lower() != username):
            continue
        try:
            ports.append(Port.from_dict(d))
        except Exception:
            continue
    return ports

def admin_scan_all_ports() -> List[Port]:
    _ensure_dirs()
    out: List[Port] = []
    for p in PORTS_DIR.glob("port_*.json"):
        d = _read_json(p, {})
        if not d:
            continue
        try:
            out.append(Port.from_dict(d))
        except Exception:
            continue
    return out

# ---------- Remaining-seconds helpers ----------
def remaining_seconds_for_port(port: "Port") -> int:
    """
    Legacy: discovered_at-based timer.
    Returns remaining seconds from discovered_at + resolve_delay_sec - now.
    """
    if not port or port.status != "discovered":
        return 0
    sec = int(getattr(port, "resolve_delay_sec", 0) or 0)
    if sec <= 0:
        return 0
    disc = getattr(port, "discovered_at", None)
    if not disc:
        return sec
    try:
        dt = datetime.fromisoformat(disc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ready_at = dt + timedelta(seconds=sec)
        now = datetime.now(timezone.utc)
        remain = int((ready_at - now).total_seconds())
        return max(0, remain)
    except Exception:
        return sec

def remaining_seconds_for_click(port: "Port") -> int:
    """
    Click-based timer:
      - If resolve_started_at is None → full delay remains
      - Else → (resolve_started_at + delay) - now
    Returns >= 0
    """
    if not port or port.status != "discovered":
        return 0
    sec = int(getattr(port, "resolve_delay_sec", 0) or 0)
    if sec <= 0:
        return 0

    started = getattr(port, "resolve_started_at", None)
    if not started:
        # never clicked yet → full delay
        return sec

    try:
        dt = datetime.fromisoformat(started)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ready_at = dt + timedelta(seconds=sec)
        now = datetime.now(timezone.utc)
        remain = int((ready_at - now).total_seconds())
        return max(0, remain)
    except Exception:
        # corrupted timestamp → conservative fallback: require full delay again
        return sec

# ---------- Domain operations ----------
def scan_user_assigned(username: str) -> int:
    """Flip assigned → discovered for this user; returns count changed."""
    changed = 0
    for port in list_ports_for_user(username):
        if port.status == "assigned":
            port.status = "discovered"
            port.discovered_at = port.discovered_at or _utcnow_iso()
            save_port(port)
            changed += 1
    return changed

def resolve_port(username: str, pid: str, idempotency_key: Optional[str] = None) -> Dict:
    """
    Resolve a discovered port for this user (single-file, lock + idempotency + delay enforcement).
    Returns:
      {ok: True} on success
      {ok: False, error: "forbidden"|"not_found"|"busy"|"invalid_state"|"too_early", ...}
    """
    if idempotency_key and idempotency_seen(idempotency_key):
        return {"ok": True, "idempotent": True, "state": "already_processed"}

    port = load_port(pid)
    if not port:
        return {"ok": False, "error": "not_found"}

    if port.owner.lower() != (username or "").lower():
        return {"ok": False, "error": "forbidden"}

    if port.status != "discovered":
        # still “assigned” or already “resolved/archived”
        return {"ok": False, "error": "invalid_state", "state": port.status}

    # ----- Enforce per-click resolve delay -----
    delay_sec = int(port.resolve_delay_sec or 0)
    if delay_sec > 0:
        # First click on this port? start timer and ask client to wait full delay.
        if not port.resolve_started_at:
            port.resolve_started_at = _utcnow_iso()
            save_port(port)
            return {"ok": False, "error": "too_early", "seconds_remaining": delay_sec}

        # Otherwise compute remaining time based on resolve_started_at
        remaining = remaining_seconds_for_click(port)
        if remaining > 0:
            return {"ok": False, "error": "too_early", "seconds_remaining": int(remaining)}

    if not acquire_lock(pid):
        return {"ok": False, "error": "busy"}  # another request is processing this port

    try:
        # Re-read after lock to avoid stale state
        fresh = load_port(pid)
        if not fresh:
            return {"ok": False, "error": "not_found"}
        if fresh.status != "discovered":
            return {"ok": False, "error": "invalid_state", "state": fresh.status}

        # All checks passed → resolve
        fresh.status = "resolved"
        fresh.resolved_at = _utcnow_iso()
        # (Optional) keep resolve_started_at for audit; or clear it:
        # fresh.resolve_started_at = None
        save_port(fresh)

        if idempotency_key:
            record_idempotency(idempotency_key, {"ok": True, "port_id": pid})

        return {"ok": True}
    finally:
        release_lock(pid)

def archive_port(username: str, pid: str) -> Dict:
    port = load_port(pid)
    if not port:
        return {"ok": False, "error": "not_found"}
    if port.owner.lower() != (username or "").lower():
        return {"ok": False, "error": "forbidden"}

    if not acquire_lock(pid):
        return {"ok": False, "error": "busy"}

    try:
        fresh = load_port(pid)
        if not fresh:
            return {"ok": False, "error": "not_found"}

        fresh.status = "archived"
        save_port(fresh)
        return {"ok": True}
    finally:
        release_lock(pid)

def unarchive_port(username: str, pid: str) -> Dict:
    port = load_port(pid)
    if not port:
        return {"ok": False, "error": "not_found"}
    if port.owner.lower() != (username or "").lower():
        return {"ok": False, "error": "forbidden"}

    if not acquire_lock(pid):
        return {"ok": False, "error": "busy"}

    try:
        fresh = load_port(pid)
        if not fresh:
            return {"ok": False, "error": "not_found"}
        if fresh.status != "archived":
            return {"ok": False, "error": "invalid_state", "state": fresh.status}

        fresh.status = "discovered"
        fresh.discovered_at = fresh.discovered_at or _utcnow_iso()
        fresh.resolve_started_at = None
        save_port(fresh)
        return {"ok": True, "port": fresh.to_dict()}
    finally:
        release_lock(pid)

# ---------- Read models ----------
def user_dashboard_view(username: str) -> Dict:
    ports = list_ports_for_user(username)
    assigned = [p for p in ports if p.status == "assigned"]
    discovered = [p for p in ports if p.status == "discovered"]
    resolved = [p for p in ports if p.status == "resolved"]
    archived = [p for p in ports if p.status == "archived"]

    total_earned = round(sum(p.reward for p in resolved), 2)

    # Available balance (subtract approved withdrawals)
    withdrawals = _read_json(WITHDRAWALS_FILE, [])
    approved_sum = 0.0
    if isinstance(withdrawals, list):
        for w in withdrawals:
            if (w.get("username", "").lower() == (username or "").lower() and
                    w.get("status") == "approved"):
                try:
                    approved_sum += float(w.get("amount_sar", 0) or 0)
                except Exception:
                    pass
    available = round(max(0.0, total_earned - approved_sum), 2)

    wallet = {
        "total_earned": total_earned,
        "available_balance": available,
    }

    if is_rayan(username):
        wallet = load_rayan_wallet(DATA_DIR, wallet)

    return {
        "assigned": assigned,
        "discovered": discovered,
        "resolved": resolved,
        "archived": archived,
        "counts": {
            "assigned": len(assigned),
            "discovered": len(discovered),
            "resolved": len(resolved),
            "archived": len(archived),
        },
        "wallet": wallet,
    }

def admin_stats_view() -> Dict:
    ports = admin_scan_all_ports()
    total_ports = len(ports)
    total_resolved = sum(1 for p in ports if p.status == "resolved")
    total_discovered = sum(1 for p in ports if p.status == "discovered")
    total_unresolved = total_ports - total_resolved
    # list non-admin usernames from users.json
    users = _read_json(USERS_FILE, [])
    usernames = sorted({u.get("username") for u in users if not u.get("is_admin")})

    return {
        "usernames": usernames,
        "totals": {
            "ports": total_ports,
            "resolved": total_resolved,
            "discovered": total_discovered,
            "unresolved": total_unresolved,
        }
    }
