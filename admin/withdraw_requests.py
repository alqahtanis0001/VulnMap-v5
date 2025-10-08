# admin/withdraw_requests.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user

from .dashboard_logic import (
    read_withdrawals,
    group_withdrawals,
    update_withdraw_status,
    count_pending,
)

bp = Blueprint("withdraw_admin", __name__, url_prefix="/admin/withdrawals")


@bp.before_request
@login_required
def _require_admin():
    if not getattr(current_user, "is_admin", False):
        abort(403)


@bp.get("/")
def list_withdrawals():
    items = read_withdrawals()
    grouped = group_withdrawals(items)
    pending = count_pending(items)
    return render_template(
        "withdraw_requests.html",
        grouped=grouped,
        pending_count=pending,
        total=len(items),
    )


@bp.post("/<int:req_id>/status")
def set_status(req_id: int):
    # support both form POST and JSON
    new_status = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        new_status = (data.get("status") or "").strip().lower()
    else:
        new_status = (request.form.get("status") or "").strip().lower()

    ok, changed = update_withdraw_status(req_id, new_status)
    if request.is_json:
        return jsonify({"ok": ok, "item": changed}), (200 if ok else 400)

    flash("تم التحديث." if ok else "فشل التحديث.", "ok" if ok else "err")
    return redirect(url_for("withdraw_admin.list_withdrawals"))
