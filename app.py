# app.py
# VulnMap-v2
# - Auth via users.json
# - User/Admin dashboards (SSR)
# - JSON endpoints to avoid page refresh for solve/archive/scan
# - Business logic in port_logic.py

from __future__ import annotations
# add near other flask imports at the top if missing
from flask import current_app
import time
import os
import re

import json
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from scripts.keep_alive import start_keep_alive, read_keepalive_status
from withdrawals_path import get_withdrawals_file

from flask import (
    Flask, render_template, request, redirect, url_for, flash, abort, jsonify
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# ---- business logic (UUID ids, status field, locking, idempotency)
from port_logic import (
    create_port,
    user_dashboard_view,
    scan_user_assigned,
    resolve_port,
    archive_port,
    admin_stats_view,
)

from admin.withdraw_requests import bp as withdraw_bp  # NEW
from admin.dashboard_logic import count_pending        # NEW
from scripts.weekly_cleanup import run_weekly_cleanup

import random as _rand

# ------------------------------ Project paths ------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOGIN_LOG_DIR = DATA_DIR / "login_activity"

USERS_FILE = DATA_DIR / "users.json"
APPROVED_IDS_FILE = DATA_DIR / "approved_ids.json"
WITHDRAWALS_FILE = get_withdrawals_file(DATA_DIR)
PROCESSED_FILE = DATA_DIR / "ports" / "processed_requests.json"  # created by port_logic if missing


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
    app.config["SECRET_KEY"] = "dev-secret-key"  # set via env in prod

    # Login manager
    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)
    app.register_blueprint(withdraw_bp)  # NEW

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

        return render_template(
            "admin.html",
            usernames=stats["usernames"],
            total_ports=stats["totals"]["ports"],
            total_resolved=stats["totals"]["resolved"],
            total_unresolved=stats["totals"]["unresolved"],
            total_discovered=stats["totals"]["discovered"],
            pending_withdrawals=pending_w,  # NEW
            keepalive_status=ka_status,   # <-- ADD THIS

        )

    return app



app = create_app()

# 540 sec = 12 minutes. Change the default as you like.
interval = int(os.getenv("KEEP_ALIVE_INTERVAL_SEC", "720"))
start_keep_alive(DATA_DIR, interval_sec=interval)


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

    return jsonify({
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "port_id": pid,
        "counts": {"discovered": len(vm["discovered"]), "resolved": len(vm["resolved"])},
        "wallet": vm["wallet"]
    }), (200 if result.get("ok") else 400)


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


# ------------------------------ Run ------------------------------
if __name__ == "__main__":
    # In production, set host/port via env vars and disable debug.
    app.run(debug=True, host="127.0.0.1", port=5000)
