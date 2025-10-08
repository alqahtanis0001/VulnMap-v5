# user/withdraw.py
# User action: create a withdrawal request (append-only file).
from __future__ import annotations

from datetime import datetime, timezone
from flask import redirect, url_for, flash, request
from flask_login import login_required, current_user

from .dashboard_logic import user_bp
from port_logic import _read_json, _write_json_atomic, WITHDRAWALS_FILE

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

@user_bp.post("/u/withdraw")
@login_required
def withdraw_request():
    # amount in SAR, positive float
    amt_raw = request.form.get("amount_sar") or ""
    idem = (request.form.get("idempotency_key") or "").strip() or None

    try:
        amount = float(amt_raw)
    except Exception:
        amount = -1.0

    if amount <= 0:
        flash("الرجاء إدخال مبلغ صحيح.", "err")
        return redirect(url_for("user.dashboard"))

    # Optional: enforce available balance check on server-side
    # We rely on the dashboard computation for display; here we accept the request.

    # Idempotency (simple approach): build a key if not provided
    key = idem or f"withdraw:{current_user.username}:{amount:.2f}"

    # Load and check duplicate (best-effort)
    withdrawals = _read_json(WITHDRAWALS_FILE, [])
    if any(
        (w.get("username","").lower()==current_user.username.lower()
         and float(w.get("amount_sar",0))==float(f"{amount:.2f}")
         and w.get("status")=="pending")
        for w in withdrawals
    ):
        flash("طلب سحب مشابه قيد المعالجة بالفعل.", "info")
        return redirect(url_for("user.dashboard"))

    # Assign a simple incremental id
    new_id = (max([int(w.get("id", 0)) for w in withdrawals] + [0]) + 1)

    withdrawals.append({
        "id": new_id,
        "username": current_user.username.lower(),
        "amount_sar": round(float(amount), 2),
        "status": "pending",
        "created_at": _utcnow_iso(),
        "key": key
    })

    _write_json_atomic(WITHDRAWALS_FILE, withdrawals)
    flash("تم إرسال طلب السحب بنجاح.", "ok")
    return redirect(url_for("user.dashboard"))
