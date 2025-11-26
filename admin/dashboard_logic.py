# admin/dashboard_logic.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from withdrawals_path import get_withdrawals_file

# Project roots relative to this file
ROOT = Path(__file__).resolve().parents[1]  # go up from admin/ -> project root
DATA_DIR = ROOT / "data"

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _write_json_atomic(path: Path, data) -> None:
    """
    Windows-safe atomic writer (same semantics as app.py/port_logic.py).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    def write_tmp():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

    import time
    for i in range(10):
        if not tmp.exists():
            write_tmp()
        try:
            os.replace(tmp, path)
            return
        except (PermissionError, FileNotFoundError):
            time.sleep(0.05 * (i + 1))
            continue
    if not tmp.exists():
        write_tmp()
    os.replace(tmp, path)

def read_withdrawals() -> List[Dict]:
    wf = get_withdrawals_file(DATA_DIR)
    try:
        items = json.loads(wf.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            return []
        return items
    except Exception:
        return []

def write_withdrawals(items: List[Dict]) -> None:
    wf = get_withdrawals_file(DATA_DIR)
    _write_json_atomic(wf, items)

def next_withdrawal_id(items: List[Dict]) -> int:
    return (max([i.get("id", 0) for i in items] + [0]) + 1)

def group_withdrawals(items: List[Dict]) -> Dict[str, List[Dict]]:
    out = {"pending": [], "approved": [], "rejected": []}
    for i in items:
        st = (i.get("status") or "").lower()
        if st not in out:
            st = "pending"
        out[st].append(i)
    # sort: newest first within each bucket
    for k in out:
        out[k].sort(key=lambda r: (r.get("created_at") or "", r.get("id", 0)), reverse=True)
    return out

def count_pending(items: Optional[List[Dict]] = None) -> int:
    if items is None:
        items = read_withdrawals()
    return sum(1 for i in items if (i.get("status") or "").lower() == "pending")

def update_withdraw_status(req_id: int, new_status: str) -> Tuple[bool, Optional[Dict]]:
    """
    Returns (ok, updated_item_or_none)
    """
    new_status = new_status.lower().strip()
    if new_status not in ("approved", "rejected"):
        return False, None

    items = read_withdrawals()
    changed = None
    for i in items:
        if int(i.get("id", 0)) == int(req_id):
            i["status"] = new_status
            # Stamp a processed_at for audit (optional)
            i["processed_at"] = _utcnow_iso()
            changed = i
            break

    if changed is None:
        return False, None

    write_withdrawals(items)
    return True, changed
