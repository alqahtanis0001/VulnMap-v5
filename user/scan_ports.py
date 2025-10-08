# user/scan_ports.py
# User actions: scan assigned → discovered, resolve discovered → resolved.
from __future__ import annotations

from flask import redirect, url_for, flash, request
from flask_login import login_required, current_user

from .dashboard_logic import user_bp
from port_logic import scan_user_assigned, resolve_port

@user_bp.post("/u/scan")
@login_required
def scan():
    changed = scan_user_assigned(current_user.username)
    if changed > 0:
        flash(f"تم اكتشاف {changed} منفذ(اً).", "ok")
    else:
        flash("لا توجد منافذ جديدة لاكتشافها.", "info")
    # PRG back to dashboard
    return redirect(url_for("user.dashboard"))

@user_bp.post("/u/resolve")
@login_required
def resolve():
    port_id = (request.form.get("port_id") or "").strip()
    idem = (request.form.get("idempotency_key") or "").strip() or None

    if not port_id:
        flash("طلب غير صالح.", "err")
        return redirect(url_for("user.dashboard"))

    result = resolve_port(current_user.username, port_id, idem)

    if result.get("ok"):
        flash("تم الحل بنجاح.", "ok")
    else:
        err = result.get("error")
        if err == "invalid_state":
            flash("لا يمكن الحل قبل الاكتشاف. قم بالفحص أولاً.", "err")
        elif err == "busy":
            flash("يتم معالجة هذا المنفذ حالياً. حاول مجدداً لحظات لاحقاً.", "info")
        elif err == "forbidden":
            flash("هذا المنفذ لا يخص حسابك.", "err")
        elif err == "not_found":
            flash("لم يتم العثور على المنفذ.", "err")
        else:
            flash("تعذر إتمام الحل.", "err")

    return redirect(url_for("user.dashboard"))
