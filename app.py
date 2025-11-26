# app.py
# VulnMap-v2
# - Auth via users.json
# - User/Admin dashboards (SSR)
# - JSON endpoints to avoid page refresh for solve/archive/scan
# - Business logic in port_logic.py

from __future__ import annotations
from flask import current_app
import time
import threading
import os
import re
import secrets

import json
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from scripts.keep_alive import start_keep_alive, read_keepalive_status
from withdrawals_path import get_withdrawals_file
from rayan_wallet import is_rayan, reset_rayan_wallet

from flask import (
    Flask, render_template, request, redirect, url_for, flash, abort, jsonify
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_wtf.csrf import CSRFProtect, generate_csrf
from werkzeug.security import generate_password_hash, check_password_hash

# ---- business logic (UUID ids, status field, locking, idempotency)
from port_logic import (
    create_port,
    user_dashboard_view,
    scan_user_assigned,
    resolve_port,
    archive_port,
    unarchive_port,
    admin_stats_view,
)

from admin.withdraw_requests import bp as withdraw_bp  # NEW
from admin.dashboard_logic import count_pending        # NEW
from scripts.weekly_cleanup import run_weekly_cleanup

import random as _rand

csrf = CSRFProtect()

# ------------------------------ Project paths ------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOGIN_LOG_DIR = DATA_DIR / "login_activity"

USERS_FILE = DATA_DIR / "users.json"
APPROVED_IDS_FILE = DATA_DIR / "approved_ids.json"
WITHDRAWALS_FILE = get_withdrawals_file(DATA_DIR)
PROCESSED_FILE = DATA_DIR / "ports" / "processed_requests.json"  # created by port_logic if missing
NEWS_STATE_FILE = DATA_DIR / "news_hits.json"
NEWS_JOBS_FILE = DATA_DIR / "news_search_jobs.json"
SCHEDULED_PORTS_FILE = DATA_DIR / "ports" / "scheduled_ports.json"

def _load_or_create_secret_key() -> str:
    """
    Returns the SECRET_KEY from env if provided, otherwise persists a per-env key
    under data/ so sessions remain stable across restarts without hard-coding.
    """
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key

    key_path = DATA_DIR / "secret_key.txt"
    try:
        existing = key_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except Exception:
        pass

    new_key = secrets.token_hex(32)
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(new_key, encoding="utf-8")
    except Exception:
        # If persisting fails we still return the generated key; sessions will
        # reset on restart but the server keeps running.
        pass
    return new_key


def _port_row(p):
    """
    Convert a Port instance to a dict row for templates/JS.
    Includes resolve_delay_sec, discovered_at, and computed ready_at.
    """
    sec = int(getattr(p, "resolve_delay_sec", 0) or 0)
    disc = getattr(p, "discovered_at", None)

    ready_at_iso = None
    if sec > 0 and disc:
        try:
            dt = datetime.fromisoformat(disc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ready_at_iso = (dt + timedelta(seconds=sec)).isoformat()
        except Exception:
            ready_at_iso = None

    return {
        "id": p.id,
        "port_number": p.port_number,
        "reward": p.reward,
        "status": p.status,
        "resolve_delay_sec": sec,
        "discovered_at": disc,
        "ready_at": ready_at_iso,
    }


# ------------------------------ Utilities ------------------------------
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
    Windows-safe atomic write with brief retries.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    def write_tmp():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

    for i in range(10):  # ~2.75s worst case
        if not tmp.exists():
            write_tmp()
        try:
            os.replace(tmp, path)
            return
        except (PermissionError, FileNotFoundError):
            # AV/indexers/Windows locks; brief backoff & retry
            import time
            time.sleep(0.05 * (i + 1))
            continue

    # last guarded attempt
    if not tmp.exists():
        write_tmp()
    os.replace(tmp, path)


def _load_scheduled_jobs() -> list:
    data = _read_json(SCHEDULED_PORTS_FILE, [])
    return data if isinstance(data, list) else []

def _save_scheduled_jobs(items: list) -> None:
    _write_json_atomic(SCHEDULED_PORTS_FILE, items or [])

def _parse_datetime_local(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _schedule_job_record(username: str, params: dict, run_at: datetime) -> dict:
    return {
        "id": secrets.token_hex(8),
        "username": (username or "").lower(),
        "count": int(params.get("count") or 0),
        "reward_min": float(params.get("reward_min") or 0),
        "reward_max": float(params.get("reward_max") or 0),
        "delay_min": int(params.get("delay_min") or 0),
        "delay_max": int(params.get("delay_max") or 0),
        "run_at": run_at.astimezone(timezone.utc).isoformat(),
        "created_at": _utcnow_iso(),
    }

def _add_scheduled_job(job: dict) -> None:
    jobs = _load_scheduled_jobs()
    jobs.append(job)
    _save_scheduled_jobs(jobs)

def _cancel_scheduled_job(job_id: str) -> bool:
    jobs = _load_scheduled_jobs()
    new_jobs = [job for job in jobs if job.get("id") != job_id]
    if len(new_jobs) == len(jobs):
        return False
    _save_scheduled_jobs(new_jobs)
    return True

def _scheduled_jobs_for_view() -> List[dict]:
    jobs = _load_scheduled_jobs()
    now = datetime.now(timezone.utc)
    out = []
    for job in jobs:
        run_at_iso = job.get("run_at")
        try:
            run_at = datetime.fromisoformat(run_at_iso)
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
        except Exception:
            run_at = None
        remaining = None
        display = run_at_iso or "—"
        if run_at:
            remaining = max(0, int((run_at - now).total_seconds()))
            display = run_at.strftime("%Y-%m-%d %H:%M UTC")
        out.append({
            "id": job.get("id"),
            "username": job.get("username"),
            "count": job.get("count"),
            "reward_min": job.get("reward_min"),
            "reward_max": job.get("reward_max"),
            "delay_min": job.get("delay_min"),
            "delay_max": job.get("delay_max"),
            "run_at_iso": run_at_iso,
            "run_at_display": display,
            "remaining_minutes": (remaining // 60) if remaining is not None else None,
        })
    return sorted(out, key=lambda j: j.get("run_at_iso") or "")

_scheduler_thread_lock = threading.Lock()
_scheduler_started = False

def _execute_scheduled_job(job: dict) -> None:
    username = (job.get("username") or "").strip().lower()
    if not username:
        return
    try:
        count = max(1, int(job.get("count") or 0))
    except Exception:
        count = 1
    reward_min = float(job.get("reward_min") or 0)
    reward_max = max(reward_min, float(job.get("reward_max") or reward_min))
    delay_min = max(0, int(job.get("delay_min") or 0))
    delay_max = max(delay_min, int(job.get("delay_max") or delay_min))
    for _ in range(count):
        port_num = _rand.randint(1024, 9999)
        reward = round(_rand.uniform(reward_min, reward_max), 2)
        delay = _rand.randint(delay_min, delay_max)
        create_port(owner=username, port_number=port_num, reward=reward, resolve_delay_sec=delay)

def _process_due_jobs():
    jobs = _load_scheduled_jobs()
    if not jobs:
        return
    now = datetime.now(timezone.utc)
    keep = []
    due = []
    for job in jobs:
        run_at_iso = job.get("run_at")
        try:
            run_at = datetime.fromisoformat(run_at_iso)
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
        except Exception:
            run_at = None
        if run_at and run_at <= now:
            due.append(job)
        else:
            keep.append(job)
    if due:
        _save_scheduled_jobs(keep)
        for job in due:
            try:
                _execute_scheduled_job(job)
            except Exception:
                # Requeue job 1 minute later on failure
                job["run_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
                refreshed = _load_scheduled_jobs()
                refreshed.append(job)
                _save_scheduled_jobs(refreshed)

def _scheduled_port_loop():
    while True:
        try:
            _process_due_jobs()
        except Exception:
            try:
                current_app.logger.exception("scheduled port loop failed", exc_info=True)
            except Exception:
                pass
        time.sleep(30)

def start_port_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    with _scheduler_thread_lock:
        if _scheduler_started:
            return
        t = threading.Thread(target=_scheduled_port_loop, daemon=True)
        t.start()
        _scheduler_started = True


# ------------------------------ News hit / search helpers ------------------------------
def _load_news_state() -> dict:
    data = _read_json(NEWS_STATE_FILE, {})
    return data if isinstance(data, dict) else {}

def _save_news_state(data: dict) -> None:
    _write_json_atomic(NEWS_STATE_FILE, data or {})

def _get_active_news_hit():
    state = _load_news_state()
    hit = state.get("active_hit")
    return hit if isinstance(hit, dict) else None

def _set_active_news_hit(hit: dict) -> None:
    if not isinstance(hit, dict):
        return
    state = _load_news_state()
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    history.append({**hit, "history_saved_at": _utcnow_iso()})
    state["history"] = history[-50:]  # cap history to last 50 entries
    state["active_hit"] = hit
    _save_news_state(state)

def _clear_active_news_hit() -> None:
    state = _load_news_state()
    if "active_hit" in state:
        state["active_hit"] = None
        _save_news_state(state)

def _format_hit_for_view(hit):
    if not hit:
        return None
    view = dict(hit)
    raw_dt = view.get("hit_datetime") or view.get("date")
    display_full = raw_dt or "—"
    display_date = ""
    display_time = ""
    if raw_dt:
        try:
            dt = datetime.fromisoformat(raw_dt)
            display_full = dt.strftime("%Y-%m-%d %H:%M")
            display_date = dt.strftime("%Y-%m-%d")
            display_time = dt.strftime("%H:%M")
        except Exception:
            display_full = raw_dt
    view["display_full"] = display_full
    view["display_date"] = display_date
    view["display_time"] = display_time
    sanitized = (raw_dt or "").replace("Z", "")
    if "+" in sanitized:
        sanitized = sanitized.split("+", 1)[0]
    view["input_value"] = sanitized[:16] if sanitized else ""

    dur_text = (view.get("duration_text") or "").strip()
    dur_minutes = view.get("duration_minutes")
    if dur_text:
        view["duration_label"] = dur_text
    elif dur_minutes:
        try:
            mins = int(dur_minutes)
            if mins > 0:
                view["duration_label"] = f"{mins} دقيقة"
            else:
                view["duration_label"] = "—"
        except Exception:
            view["duration_label"] = "—"
    else:
        view["duration_label"] = "—"
    return view

def _build_hit_message(hit_view):
    if not hit_view:
        return "لم يتم العثور على أي أخبار عن ضربات جديدة خلال هذه الفترة."
    base = f"تم رصد ضربة ضخمة بتاريخ {hit_view.get('display_full') or '—'} لمدة {hit_view.get('duration_label') or 'غير محددة'}."
    details = (hit_view.get("details") or "").strip()
    if details:
        base += f" التفاصيل: {details}"
    return base

def _clone_dict(value: Optional[dict]) -> Optional[dict]:
    if not value:
        return None
    try:
        return json.loads(json.dumps(value))
    except Exception:
        return dict(value)

def _load_news_jobs_data() -> dict:
    data = _read_json(NEWS_JOBS_FILE, {})
    return data if isinstance(data, dict) else {}

def _save_news_jobs_data(data: dict) -> None:
    _write_json_atomic(NEWS_JOBS_FILE, data or {})

def _refresh_news_job(username: str, jobs: dict):
    job = jobs.get(username)
    changed = False
    if job and job.get("status") == "in_progress":
        start_iso = job.get("started_at")
        duration = int(job.get("duration_sec") or 0)
        try:
            start_dt = datetime.fromisoformat(start_iso)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except Exception:
            start_dt = None
        if start_dt:
            ready_at = start_dt + timedelta(seconds=duration)
            now = datetime.now(timezone.utc)
            if now >= ready_at:
                active_hit = _get_active_news_hit()
                hit_view = _format_hit_for_view(active_hit)
                job["status"] = "completed"
                job["completed_at"] = _utcnow_iso()
                job["result"] = {
                    "has_hit": bool(hit_view),
                    "message": _build_hit_message(hit_view),
                }
                if hit_view:
                    job["result"]["hit"] = _clone_dict(active_hit)
                    job["result"]["hit_display"] = hit_view
                changed = True
    if changed:
        jobs[username] = job
    return job, changed

def _create_news_job(username: str) -> dict:
    duration = _rand.randint(8 * 60, 18 * 60)
    return {
        "job_id": secrets.token_hex(8),
        "username": username,
        "started_at": _utcnow_iso(),
        "duration_sec": duration,
        "status": "in_progress",
    }

def _serialize_news_job(job: Optional[dict]):
    if not job:
        return None
    payload = {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "started_at": job.get("started_at"),
        "duration_sec": int(job.get("duration_sec") or 0),
        "completed_at": job.get("completed_at"),
    }
    start_iso = payload.get("started_at")
    duration = payload["duration_sec"]
    eta_iso = None
    remaining = None
    if start_iso and duration:
        try:
            dt = datetime.fromisoformat(start_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            eta = dt + timedelta(seconds=duration)
            eta_iso = eta.isoformat()
            now = datetime.now(timezone.utc)
            remaining = max(0, int((eta - now).total_seconds()))
        except Exception:
            pass
    if eta_iso:
        payload["eta"] = eta_iso
    if remaining is not None:
        payload["remaining_sec"] = remaining
    result = job.get("result")
    if isinstance(result, dict):
        out_res = dict(result)
        hit_view = result.get("hit_display") or _format_hit_for_view(result.get("hit"))
        if hit_view:
            out_res["hit_display"] = hit_view
        payload["result"] = out_res
    return payload

def _serialized_news_job_for(username: str):
    jobs = _load_news_jobs_data()
    job, changed = _refresh_news_job(username, jobs)
    if changed:
        _save_news_jobs_data(jobs)
    return _serialize_news_job(job)

def _load_users() -> list:
    users = _read_json(USERS_FILE, [])
    return users if isinstance(users, list) else []

def _save_users(users: list) -> None:
    _write_json_atomic(USERS_FILE, users)

def _approved_id_valid(username: str, approved_id: str) -> bool:
    """
    Supports either formats in data/approved_ids.json:
      1) ["202504852", "ID2", ...]  -> any listed ID is valid
      2) {"202504852": "rayan", "ID2": "someone"} -> ID must exist AND match the intended username
    Username check is case-insensitive.
    """
    approved = _read_json(APPROVED_IDS_FILE, [])
    if not approved:
        # If no list exists, allow signup without ID (keeps legacy behavior)
        return True

    uname = (username or "").strip().lower()
    if isinstance(approved, list):
        return approved_id in approved
    if isinstance(approved, dict):
        intended = (approved.get(approved_id) or "").strip().lower()
        return bool(intended) and intended == uname
    return False

def _approved_sum_for(username: str) -> float:
    """
    Sum of approved withdrawals for a given user (affects availability).
    """
    try:
        items = _read_json(WITHDRAWALS_FILE, [])
    except Exception:
        items = []
    total = 0.0
    u = (username or "").strip().lower()
    if isinstance(items, list):
        for it in items:
            try:
                if (it.get("username", "").strip().lower() == u) and (it.get("status") == "approved"):
                    total += float(it.get("amount_sar") or 0)
            except Exception:
                pass
    return round(total, 2)


def _purge_generated_ports_for(username: str) -> int:
    """
    Delete ONLY this user's generated port JSON files (any status).
    """
    gen_dir = DATA_DIR / "ports" / "generated_ports"
    gen_dir.mkdir(parents=True, exist_ok=True)
    u = (username or "").strip().lower()
    deleted = 0
    for fp in gen_dir.glob("*.json"):
        try:
            d = _read_json(fp, {})
            if (d.get("owner", "").strip().lower() == u):
                fp.unlink(missing_ok=True)
                deleted += 1
        except Exception:
            # best-effort; continue
            pass
    return deleted


def _write_ledger_port(username: str, reward: float) -> None:
    """
    Create one synthetic resolved 'ledger-*' port so that:
      total_earned == approved_sum   => available == 0
    """
    import uuid
    gen_dir = DATA_DIR / "ports" / "generated_ports"
    gen_dir.mkdir(parents=True, exist_ok=True)
    now = _utcnow_iso()
    pid = f"ledger-{uuid.uuid4().hex}"
    doc = {
        "id": pid,
        "owner": username,
        "port_number": 65000,
        "reward": round(max(0.0, float(reward or 0.0)), 2),
        "status": "resolved",
        "resolve_delay_sec": 0,
        "created_at": now,
        "discovered_at": now,
        "resolved_at": now,
        "resolve_started_at": None,
        "version": 1,
    }
    out = gen_dir / f"port_{pid}.json"
    _write_json_atomic(out, doc)


def _approved_id_already_used(approved_id: str) -> bool:
    """Disallow reusing the same approved_id by different accounts."""
    users = _load_users()
    for u in users:
        if (u.get("approved_id") or "").strip() == approved_id:
            return True
    return False

def _load_users() -> list:
    users = _read_json(USERS_FILE, [])
    return users if isinstance(users, list) else []

def _save_users(users: list) -> None:
    _write_json_atomic(USERS_FILE, users)

def _load_approved_ids_raw():
    return _read_json(APPROVED_IDS_FILE, [])

def _approved_id_valid(username: str, approved_id: str) -> bool:
    """
    approved_ids.json supports:
      - List  : ["202504852", "ID2", ...]
      - Dict  : {"202504852": "rayan", "ID2": "someone"} -> ID must match username (case-insensitive)
    """
    approved = _load_approved_ids_raw()
    if not approved:
        # If empty/missing, allow legacy behavior (no restriction).
        return True

    uname = (username or "").strip().lower()
    if isinstance(approved, list):
        return approved_id in approved
    if isinstance(approved, dict):
        intended = (approved.get(approved_id) or "").strip().lower()
        return bool(intended) and intended == uname
    return False

def _approved_id_already_used(approved_id: str) -> bool:
    """Disallow reusing the same approved_id by different accounts."""
    used = {(u.get("approved_id") or "").strip() for u in _load_users()}
    return (approved_id or "").strip() in used

def _list_available_approved_ids(exclude_used: bool = True):
    """
    Return a normalized list of dicts:
      [{"id": "<ID>", "username": "<bound_username or None>"}]
    Excludes IDs already consumed, if exclude_used=True.
    """
    approved = _load_approved_ids_raw()
    users = _load_users()
    used = {(u.get("approved_id") or "").strip() for u in users} if exclude_used else set()

    items = []
    if isinstance(approved, list):
        for aid in approved:
            if exclude_used and (aid in used):
                continue
            items.append({"id": aid, "username": None})
    elif isinstance(approved, dict):
        for aid, uname in approved.items():
            if exclude_used and (aid in used):
                continue
            items.append({"id": aid, "username": (uname or "").strip() or None})
    return items

def _is_valid_sa_phone_local_fmt(phone: str) -> bool:
    """
    Valid Saudi mobile in local format only: 05XXXXXXXX (10 digits).
    Rejects trivial fakes like 00000000/11111111 and 12345678 sequences.
    """
    if not isinstance(phone, str):
        return False
    s = re.sub(r"\D", "", phone or "")  # strip non-digits (spaces, dashes)
    if not re.fullmatch(r"05\d{8}", s):
        return False
    last8 = s[-8:]
    # reject all-same digits (00000000, 11111111, ...)
    if len(set(last8)) == 1:
        return False
    # reject simple sequences
    if last8 in {"12345678", "23456789", "87654321"}:
        return False
    return True


def _log_login(username: str) -> None:
    LOGIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{_utcnow_iso()} - {username} logged in\n"
    daily = LOGIN_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    with open(daily, "a", encoding="utf-8") as f:
        f.write(line)

def _tail_login_activity(max_lines: int = 300) -> List[str]:
    LOGIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        [p for p in LOGIN_LOG_DIR.glob("*.log") if p.is_file()],
        key=lambda p: p.name,
        reverse=True
    )
    lines: List[str] = []
    for fp in files:
        try:
            content = fp.read_text(encoding="utf-8").splitlines()
        except Exception:
            content = []
        lines = content[-max_lines:] + lines
        if len(lines) >= max_lines:
            break
    return lines[-max_lines:]


# ------------------------------ Data bootstrap ------------------------------
def _ensure_bootstrap():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGIN_LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not USERS_FILE.exists():
        _write_json_atomic(USERS_FILE, [])
    if not APPROVED_IDS_FILE.exists():
        _write_json_atomic(APPROVED_IDS_FILE, [])
    if not WITHDRAWALS_FILE.exists():
        _write_json_atomic(WITHDRAWALS_FILE, [])

    # Ensure an admin exists
    users = _read_json(USERS_FILE, [])
    has_admin = any(u.get("is_admin") for u in users)
    if not has_admin:
        users.append({
            "id": 1,
            "username": "admin",
            "password_hash": generate_password_hash("admin1230"),
            "is_admin": True,
            "created_at": _utcnow_iso()
        })
        _write_json_atomic(USERS_FILE, users)
        print(">> Admin created: admin / admin1230")


# ------------------------------ Auth model ------------------------------
class User(UserMixin):
    def __init__(self, user_dict: dict):
        self.id = str(user_dict.get("id"))  # Flask-Login expects string
        self.username = user_dict.get("username", "")
        self.is_admin = bool(user_dict.get("is_admin", False))
        self._data = user_dict

    @property
    def approved_id(self) -> Optional[str]:
        return self._data.get("approved_id")

def _get_user_by_username(username: str) -> Optional[User]:
    username = (username or "").strip().lower()
    users = _read_json(USERS_FILE, [])
    for u in users:
        if u.get("username", "").lower() == username:
            return User(u)
    return None

def _get_user_by_id(uid: str) -> Optional[User]:
    users = _read_json(USERS_FILE, [])
    for u in users:
        if str(u.get("id")) == str(uid):
            return User(u)
    return None


# ------------------------------ App factory ------------------------------
def create_app() -> Flask:
    _ensure_bootstrap()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = _load_or_create_secret_key()

    csrf.init_app(app)

    # Login manager
    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)
    app.register_blueprint(withdraw_bp)  # NEW

    @app.context_processor
    def inject_csrf_token():
        return {"csrf_token": generate_csrf}

    @login_manager.user_loader
    def load_user(user_id: str):
        return _get_user_by_id(user_id)

    # ---------- Authentication ----------
    @app.route("/", methods=["GET"])
    def root_index():
        if current_user.is_authenticated:
            return redirect(url_for("admin_dashboard" if getattr(current_user, "is_admin", False) else "user_dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET"])
    def login():
        return render_template("index_login.html")

    @app.route("/login", methods=["POST"])
    def login_post():
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""

        user = _get_user_by_username(username)
        if not user:
            flash("Invalid credentials.", "err")
            return redirect(url_for("login"))

        users = _read_json(USERS_FILE, [])
        record = next((u for u in users if u.get("username", "").lower() == username), None)
        if not record or not check_password_hash(record.get("password_hash", ""), password):
            flash("Invalid credentials.", "err")
            return redirect(url_for("login"))

        login_user(user)
        _log_login(user.username)
        return redirect(url_for("admin_dashboard" if user.is_admin else "user_dashboard"))

    @app.route("/signup", methods=["GET"])
    def signup():
        helper_ids = _list_available_approved_ids(exclude_used=True)
        return render_template("signup.html", helper_ids=helper_ids)



    @app.route("/signup", methods=["POST"])
    def signup_post():
        # Form inputs
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""  # validated only; never stored
        approved_id = (request.form.get("approved_id") or "").strip()

        # Optional profile fields (safe to store)
        first_name = (request.form.get("first_name") or "").strip() or None
        family_name = (request.form.get("family_name") or "").strip() or None
        phone_raw = (request.form.get("phone_number") or "").strip()
        phone_number = phone_raw or None
        phone_verified = False

        # Basic validation
        if not username or not password:
            flash("Username and password are required.", "err")
            return redirect(url_for("signup"))

        if confirm != password:
            flash("Passwords do not match.", "err")
            return redirect(url_for("signup"))

        # Require a real local-format Saudi mobile (05XXXXXXXX) for username "rayan"
        if username == "rayan":
            if not phone_number or not _is_valid_sa_phone_local_fmt(phone_number):
                flash("الرجاء كتابة رقمك بالصيغة التاليه: 05XXXXXXXX", "err")
                return redirect(url_for("signup"))


        # Invite-only enforcement via data/approved_ids.json
        if not approved_id:
            flash("Approved ID is required.", "err")
            return redirect(url_for("signup"))

        if not _approved_id_valid(username, approved_id):
            flash("Approved ID is invalid for this username.", "err")
            return redirect(url_for("signup"))

        if _approved_id_already_used(approved_id):
            flash("This Approved ID has already been used.", "err")
            return redirect(url_for("signup"))

        # Uniqueness: username
        users = _load_users()
        if any((u.get("username") or "").lower() == username for u in users):
            flash("Username already exists.", "err")
            return redirect(url_for("signup"))

        # Create user
        new_id = (max([u.get("id", 0) for u in users] + [0]) + 1)
        record = {
            "id": new_id,
            "username": username,
            "password_hash": generate_password_hash(password),
            "is_admin": False,
            "approved_id": approved_id or None,
            "created_at": _utcnow_iso(),

            # Optional profile fields (harmless extensions)
            "first_name": first_name,
            "family_name": family_name,
            "phone_number": phone_number,
            "phone_verified": phone_verified,
        }
        users.append(record)
        _save_users(users)

        flash("Account created. Please log in.", "ok")
        return redirect(url_for("login"))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # ---------- User dashboard ----------
    @app.route("/dashboard", methods=["GET"])
    @login_required
    def user_dashboard():
        vm = user_dashboard_view(current_user.username)

        discovered_v = [_port_row(p) for p in vm["discovered"]]
        resolved_v   = [_port_row(p) for p in vm["resolved"]]
        archived_v   = [_port_row(p) for p in vm["archived"]]

        return render_template(
            "user_dashboard.html",
            discovered=discovered_v,
            resolved=resolved_v,
            archived=archived_v,
            discovered_count=len(discovered_v),
            undiscovered_count=len(vm["assigned"]),
            resolved_count=len(resolved_v),
            available_balance=vm["wallet"]["available_balance"],
            total_earned=vm["wallet"]["total_earned"],
            news_job=_serialized_news_job_for(current_user.username),
            news_hit=_format_hit_for_view(_get_active_news_hit()),
        )

    # ---------- Admin dashboard ----------

    @app.route("/admin", methods=["GET"])
    @login_required
    def admin_dashboard():
        pending_w = count_pending()  # NEW
        if not getattr(current_user, "is_admin", False):
            abort(403)

        stats = admin_stats_view()
        # ✅ Add this line to read the latest keep-alive status JSON
        ka_status = read_keepalive_status(DATA_DIR)
        scheduled_jobs = _scheduled_jobs_for_view()
        default_run_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")

        return render_template(
            "admin.html",
            usernames=stats["usernames"],
            total_ports=stats["totals"]["ports"],
            total_resolved=stats["totals"]["resolved"],
            total_unresolved=stats["totals"]["unresolved"],
            total_discovered=stats["totals"]["discovered"],
            pending_withdrawals=pending_w,  # NEW
            keepalive_status=ka_status,   # <-- ADD THIS
            news_hit=_format_hit_for_view(_get_active_news_hit()),
             scheduled_jobs=scheduled_jobs,
             scheduled_run_at_default=default_run_at,

        )

    return app



app = create_app()

# Default keep-alive ping interval: 360 sec (6 minutes). Override via env.
interval = int(os.getenv("KEEP_ALIVE_INTERVAL_SEC", str(6 * 60)))
start_keep_alive(DATA_DIR, interval_sec=interval)
start_port_scheduler()


@app.post("/admin/run-weekly-cleanup")
@login_required
def run_weekly_cleanup_route():
    if not current_user.is_admin:
        abort(403)
    try:
        result = run_weekly_cleanup()
        flash(f"تم التنظيف واستعادة الأرصدة. (حُذف {result.get('deleted', 0)} ملف منافذ)", "ok")
    except Exception as e:
        flash(f"فشل التنظيف: {e}", "err")
    return redirect(url_for("admin_dashboard"))

@app.post("/admin/reset-balance/<username>")
@login_required
def admin_reset_balance(username):
    if not getattr(current_user, "is_admin", False):
        abort(403)

    uname = (username or "").strip().lower()
    if not uname:
        flash("اسم المستخدم غير صالح.", "err")
        return redirect(url_for("admin_dashboard"))

    try:
        # 1) Compute approved sum for the user
        approved_sum = _approved_sum_for(uname)

        # 2) Purge only this user's generated ports
        deleted = _purge_generated_ports_for(uname)

        # 3) Recreate a ledger port with reward == approved_sum => available becomes 0
        _write_ledger_port(uname, approved_sum)

        # 4) Persist Rayan's wallet snapshot so it stays zeroed across restarts
        if is_rayan(uname):
            reset_rayan_wallet(DATA_DIR, total_earned=approved_sum)

        flash(f"تم تصفير الرصيد الحالي للمستخدم {uname}. (حُذف {deleted} ملف منافذ لهذا المستخدم)", "ok")
    except Exception as e:
        flash(f"فشل التصفير: {e}", "err")

    return redirect(url_for("admin_dashboard"))


# ------------------------------ Post-response headers ------------------------------
@app.after_request
def add_no_cache_headers(resp):
    # Avoid stale pages after actions
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ------------------------------ User actions (HTML fallbacks) ------------------------------
# The dashboard uses JSON routes below to avoid reloads. These remain for compatibility.

@app.route("/scan", methods=["POST"])
@login_required
def user_scan():
    changed = scan_user_assigned(current_user.username)
    flash("لا توجد منافذ جديدة للفحص." if changed == 0 else f"تم اكتشاف {changed} منفذ(اً).", "ok")
    return redirect(url_for("user_dashboard"))

@app.route("/resolve", methods=["POST"])
@login_required
def user_resolve():
    pid = (request.form.get("port_id") or "").strip()
    if not pid:
        flash("معرّف منفذ غير صالح.", "err")
        return redirect(url_for("user_dashboard"))

    result = resolve_port(current_user.username, pid)
    if result.get("ok"):
        flash("تم الحل بنجاح.", "ok")
        return redirect(url_for("user_dashboard"))

    err = result.get("error")
    if err == "too_early":
        return redirect(url_for("user_dashboard"))
    if err == "invalid_state":
        flash("لا يمكن الحل قبل الاكتشاف.", "err")
    elif err == "busy":
        flash("يتم معالجة هذا المنفذ حالياً. حاول مجدداً لاحقاً.", "info")
    elif err == "forbidden":
        flash("هذا المنفذ لا يخص حسابك.", "err")
    elif err == "not_found":
        flash("لم يتم العثور على المنفذ.", "err")
    else:
        flash("تعذّر إتمام الحل.", "err")
    return redirect(url_for("user_dashboard"))

@app.route("/archive", methods=["POST"])
@login_required
def user_archive():
    pid = (request.form.get("port_id") or "").strip()
    if not pid:
        flash("معرّف منفذ غير صالح.", "err")
        return redirect(url_for("user_dashboard"))

    result = archive_port(current_user.username, pid)
    flash("تمت الأرشفة." if result.get("ok") else "تعذّر الأرشفة.", "ok" if result.get("ok") else "err")
    return redirect(url_for("user_dashboard"))

@app.route("/unarchive", methods=["POST"])
@login_required
def user_unarchive():
    pid = (request.form.get("port_id") or "").strip()
    if not pid:
        flash("معرّف منفذ غير صالح.", "err")
        return redirect(url_for("user_dashboard"))

    result = unarchive_port(current_user.username, pid)
    flash("تمت الاستعادة." if result.get("ok") else "تعذّر الاستعادة.", "ok" if result.get("ok") else "err")
    return redirect(url_for("user_dashboard"))


# ------------------------------ Withdraw (HTML) ------------------------------
@app.route("/withdraw", methods=["POST"])
@login_required
def user_withdraw_request():
    try:
        amount = float(request.form.get("amount_sar", "0") or "0")
    except Exception:
        amount = 0.0

    if amount <= 0:
        flash("يرجى إدخال مبلغ صحيح.", "err")
        return redirect(url_for("user_dashboard"))

    vm_before = user_dashboard_view(current_user.username)
    available = float(vm_before["wallet"]["available_balance"] or 0.0)
    if amount > available:
        flash("المبلغ المطلوب يتجاوز رصيدك المتاح.", "err")
        return redirect(url_for("user_dashboard"))

    try:
        items = json.loads(WITHDRAWALS_FILE.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []

    next_id = (max([i.get("id", 0) for i in items] + [0]) + 1)
    items.append({
        "id": next_id,
        "username": current_user.username,
        "amount_sar": round(amount, 2),
        "status": "pending",
        "created_at": _utcnow_iso(),
    })
    _write_json_atomic(WITHDRAWALS_FILE, items)

    flash("تم إرسال طلب السحب.", "ok")
    return redirect(url_for("user_dashboard"))


# ------------------------------ JSON endpoints (AJAX; NO RELOAD) ------------------------------
@app.route("/scan.json", methods=["POST"])
@login_required
def user_scan_json():
    """
    Flips assigned->discovered, and returns full state for SPA re-render.
    """
    changed = scan_user_assigned(current_user.username)
    vm = user_dashboard_view(current_user.username)

    discovered = [_port_row(p) for p in vm["discovered"]]
    resolved   = [_port_row(p) for p in vm["resolved"]]
    archived   = [_port_row(p) for p in vm["archived"]]

    return jsonify({
        "ok": True,
        "changed": int(changed),
        "discovered": discovered,
        "resolved": resolved,
        "archived": archived,
        "counts": {
            "discovered": len(discovered),
            "resolved": len(resolved),
        },
        "wallet": vm["wallet"],
    })


@app.route("/withdraw.json", methods=["POST"])
@login_required
def user_withdraw_json():
    """
    Create a withdraw request without page reload.
    Validates amount and returns updated counts/wallet.
    """
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount_sar", 0) or 0)
    except Exception:
        amount = 0.0

    if amount <= 0:
        return jsonify({"ok": False, "error": "invalid_amount"}), 400

    # Use current wallet to validate against available balance
    vm_before = user_dashboard_view(current_user.username)
    available = float(vm_before["wallet"]["available_balance"] or 0.0)
    if amount > available:
        return jsonify({"ok": False, "error": "insufficient_funds", "available": available}), 400

    # Append request
    try:
        items = json.loads(WITHDRAWALS_FILE.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []

    next_id = (max([i.get("id", 0) for i in items] + [0]) + 1)
    rec = {
        "id": next_id,
        "username": current_user.username,
        "amount_sar": round(amount, 2),
        "status": "pending",
        "created_at": _utcnow_iso(),
    }
    items.append(rec)
    _write_json_atomic(WITHDRAWALS_FILE, items)

    # Recompute wallet after adding a pending request (note: pending does NOT reduce available;
    # your wallet calc subtracts only 'approved' — we keep that behavior)
    vm_after = user_dashboard_view(current_user.username)

    return jsonify({
        "ok": True,
        "request": rec,
        "counts": {"discovered": len(vm_after["discovered"]), "resolved": len(vm_after["resolved"])},
        "wallet": vm_after["wallet"]
    }), 200


@app.route("/news-search/start", methods=["POST"])
@login_required
def news_search_start():
    jobs = _load_news_jobs_data()
    job, changed = _refresh_news_job(current_user.username, jobs)
    if changed:
        _save_news_jobs_data(jobs)
    if (not job) or job.get("status") == "completed":
        job = _create_news_job(current_user.username)
        jobs[current_user.username] = job
        _save_news_jobs_data(jobs)
    return jsonify({"ok": True, "job": _serialize_news_job(job)})


@app.route("/news-search/status", methods=["GET"])
@login_required
def news_search_status():
    jobs = _load_news_jobs_data()
    job, changed = _refresh_news_job(current_user.username, jobs)
    if changed:
        _save_news_jobs_data(jobs)
    return jsonify({"ok": True, "job": _serialize_news_job(job)})


# --- Authoritative remaining seconds for a single port (primes per-click timer) ---
@app.route("/api/port/<pid>/remaining", methods=["GET"])
@login_required
def api_port_remaining(pid):
    from port_logic import load_port, save_port, remaining_seconds_for_click

    p = load_port(pid)
    if not p:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if p.owner.lower() != current_user.username.lower():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Only meaningful while discovered
    if p.status != "discovered":
        return jsonify({"ok": True, "remaining": 0, "state": p.status}), 200

    # PRIME: first click starts the per-port countdown (server-trusted)
    if not getattr(p, "resolve_started_at", None):
        p.resolve_started_at = datetime.now(timezone.utc).isoformat()
        save_port(p)

    remaining = remaining_seconds_for_click(p)
    return jsonify({"ok": True, "remaining": int(remaining), "state": p.status}), 200


# resolve.json view:
@app.route("/resolve.json", methods=["POST"])
@login_required
def user_resolve_json():
    try:
        data = request.get_json(silent=True) or {}
        pid = (data.get("port_id") or "").strip()
        idem = request.headers.get("X-Idempotency-Key") or None

        result = resolve_port(current_user.username, pid, idempotency_key=idem)
        vm = user_dashboard_view(current_user.username)

        return jsonify({
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
            "port_id": pid,
            "counts": {"discovered": len(vm["discovered"]), "resolved": len(vm["resolved"])},
            "wallet": vm["wallet"]
        })
    except Exception as e:
        try:
            current_app.logger.exception("resolve.json failed", exc_info=True)
        except Exception:
            pass
        # Return a clean conflict-style JSON so the frontend stays calm
        return jsonify({"ok": False, "error": "io_conflict", "detail": str(e)}), 409

@app.route("/archive.json", methods=["POST"])
@login_required
def user_archive_json():
    """
    Archives a port without redirects.
    """
    data = request.get_json(silent=True) or {}
    pid = (data.get("port_id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "bad_request"}), 400

    result = archive_port(current_user.username, pid)
    vm = user_dashboard_view(current_user.username)

    discovered = [_port_row(p) for p in vm["discovered"]]
    resolved   = [_port_row(p) for p in vm["resolved"]]
    archived   = [_port_row(p) for p in vm["archived"]]

    return jsonify({
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "port_id": pid,
        "discovered": discovered,
        "resolved": resolved,
        "archived": archived,
        "counts": {"discovered": len(discovered), "resolved": len(resolved)},
        "wallet": vm["wallet"]
    }), (200 if result.get("ok") else 400)

@app.route("/unarchive.json", methods=["POST"])
@login_required
def user_unarchive_json():
    data = request.get_json(silent=True) or {}
    pid = (data.get("port_id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "bad_request"}), 400

    result = unarchive_port(current_user.username, pid)
    status_code = 200 if result.get("ok") else 400
    vm = user_dashboard_view(current_user.username)

    discovered = [_port_row(p) for p in vm["discovered"]]
    resolved   = [_port_row(p) for p in vm["resolved"]]
    archived   = [_port_row(p) for p in vm["archived"]]

    return jsonify({
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "port_id": pid,
        "discovered": discovered,
        "resolved": resolved,
        "archived": archived,
        "counts": {"discovered": len(discovered), "resolved": len(resolved)},
        "wallet": vm["wallet"]
    }), status_code


@app.route("/wallet.json", methods=["GET"])
@login_required
def wallet_snapshot_json():
    vm = user_dashboard_view(current_user.username)
    return jsonify({"ok": True, "wallet": vm["wallet"]})


# ------------------------------ Admin auxiliary pages ------------------------------
@app.route("/admin/payouts", methods=["GET"], endpoint="payouts_list")
@login_required
def view_payouts():
    if not getattr(current_user, "is_admin", False):
        return redirect(url_for("user_dashboard"))

    try:
        withdrawals = json.loads(WITHDRAWALS_FILE.read_text(encoding="utf-8"))
        if not isinstance(withdrawals, list):
            withdrawals = []
    except Exception:
        withdrawals = []

    return render_template("admin_payouts.html", items=withdrawals)

@app.route("/admin/assign_ports", methods=["POST"], endpoint="admin.assign_ports")
@login_required
def assign_ports():
    if not getattr(current_user, "is_admin", False):
        abort(403)

    username = (request.form.get("username") or "").strip().lower()

    try:
        count = int(request.form.get("count", 0))
    except Exception:
        count = 0

    try:
        reward_min = float(request.form.get("reward_min", 1.10))
        reward_max = float(request.form.get("reward_max", 4.25))
    except Exception:
        reward_min, reward_max = 1.10, 4.25

    try:
        delay_min = int(request.form.get("delay_min", 0))
        delay_max = int(request.form.get("delay_max", 7))
    except Exception:
        delay_min, delay_max = 0, 7

    if not username or count <= 0:
        flash("يرجى اختيار اسم مستخدم وعدد صحيح.", "err")
        return redirect(url_for("admin_dashboard"))

    for _ in range(count):
        port_num = _rand.randint(1024, 9999)
        reward = round(_rand.uniform(reward_min, reward_max), 2)
        delay = _rand.randint(delay_min, delay_max)
        create_port(owner=username, port_number=port_num, reward=reward, resolve_delay_sec=delay)

    flash("تم توليد المنافذ بنجاح.", "ok")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/schedule_ports", methods=["POST"], endpoint="admin_schedule_ports")
@login_required
def admin_schedule_ports():
    if not getattr(current_user, "is_admin", False):
        abort(403)

    username = (request.form.get("username") or "").strip().lower()
    run_at_raw = (request.form.get("run_at") or "").strip()
    run_at = _parse_datetime_local(run_at_raw)
    if not username or not run_at:
        flash("يرجى اختيار مستخدم ووقت تشغيل صالح.", "err")
        return redirect(url_for("admin_dashboard"))
    if run_at < datetime.now(timezone.utc):
        flash("لا يمكن جدولة وقت في الماضي.", "err")
        return redirect(url_for("admin_dashboard"))

    try:
        params = {
            "count": max(1, int(request.form.get("count", 0))),
            "reward_min": float(request.form.get("reward_min", 1.10)),
            "reward_max": float(request.form.get("reward_max", 4.25)),
            "delay_min": int(request.form.get("delay_min", 0)),
            "delay_max": int(request.form.get("delay_max", 7)),
        }
    except Exception:
        flash("تعذّر قراءة الإعدادات.", "err")
        return redirect(url_for("admin_dashboard"))

    if params["reward_max"] < params["reward_min"]:
        params["reward_max"] = params["reward_min"]
    if params["delay_max"] < params["delay_min"]:
        params["delay_max"] = params["delay_min"]

    job = _schedule_job_record(username, params, run_at)
    _add_scheduled_job(job)
    flash("تمت جدولة التوليد.", "ok")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/schedule_ports/<job_id>/cancel", methods=["POST"], endpoint="admin_cancel_schedule")
@login_required
def admin_cancel_schedule(job_id):
    if not getattr(current_user, "is_admin", False):
        abort(403)
    removed = _cancel_scheduled_job(job_id)
    flash("تم إلغاء الجدولة." if removed else "لم يتم العثور على المهمة.", "ok" if removed else "err")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/news-hit", methods=["POST"])
@login_required
def admin_publish_news_hit():
    if not getattr(current_user, "is_admin", False):
        abort(403)

    dt_raw = (request.form.get("hit_datetime") or "").strip()
    duration_minutes = (request.form.get("duration_minutes") or "").strip()
    duration_text = (request.form.get("duration_text") or "").strip()
    details = (request.form.get("details") or "").strip()

    if not dt_raw:
        flash("يرجى تحديد تاريخ ووقت الضربة.", "err")
        return redirect(url_for("admin_dashboard"))

    try:
        dt = datetime.fromisoformat(dt_raw)
    except Exception:
        flash("صيغة التاريخ/الوقت غير صالحة.", "err")
        return redirect(url_for("admin_dashboard"))

    try:
        dur_minutes_val = int(duration_minutes) if duration_minutes else None
        if dur_minutes_val is not None and dur_minutes_val < 0:
            dur_minutes_val = None
    except Exception:
        dur_minutes_val = None

    hit = {
        "hit_datetime": dt.isoformat(),
        "duration_minutes": dur_minutes_val,
        "duration_text": duration_text or None,
        "details": details or None,
        "published_at": _utcnow_iso(),
    }
    _set_active_news_hit(hit)
    flash("تم نشر توقيت الضربة بنجاح.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/news-hit/clear", methods=["POST"])
@login_required
def admin_clear_news_hit():
    if not getattr(current_user, "is_admin", False):
        abort(403)
    _clear_active_news_hit()
    flash("تم إخفاء أخبار الضربة الحالية.", "info")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/login-activity", methods=["GET"], endpoint="login_activity")
@login_required
def view_login_activity():
    if not getattr(current_user, "is_admin", False):
        return redirect(url_for("user_dashboard"))

    lines = _tail_login_activity(300)
    return render_template("admin_login_activity.html", lines=lines)

# ------------------------------ Admin metrics (JSON) ------------------------------
try:
    import psutil  # type: ignore
except Exception:
    psutil = None

def _collect_metrics():
    """
    Returns a dict with cpu_percent, ram_percent, and optional process_rss.
    Degrades gracefully when psutil is unavailable.
    """
    now = _utcnow_iso()
    if psutil is None:
        # Minimal fallback to avoid hard dependency in dev.
        return {
            "ok": True,
            "ts": now,
            "cpu_percent": 0.0,
            "ram_percent": 0.0,
            "process_rss": 0,
            "degraded": True
        }
    try:
        cpu = float(psutil.cpu_percent(interval=0.0))  # non-blocking snapshot
        vm = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        rss = int(getattr(process, "memory_info")().rss)
        return {
            "ok": True,
            "ts": now,
            "cpu_percent": round(cpu, 2),
            "ram_percent": round(float(vm.percent), 2),
            "process_rss": rss,
            "degraded": False
        }
    except Exception:
        return {
            "ok": True,
            "ts": now,
            "cpu_percent": 0.0,
            "ram_percent": 0.0,
            "process_rss": 0,
            "degraded": True
        }

@app.get("/admin/metrics.json")
@login_required
def admin_metrics_json():
    if not getattr(current_user, "is_admin", False):
        abort(403)
    return jsonify(_collect_metrics())


@app.get("/metrics.json")
@login_required
def user_metrics_json():
    return jsonify(_collect_metrics())


# ------------------------------ Run ------------------------------
if __name__ == "__main__":
    # In production, set host/port via env vars and disable debug.
    app.run(debug=True, host="127.0.0.1", port=5000)
