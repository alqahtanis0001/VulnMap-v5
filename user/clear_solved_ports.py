# user/clear_solved_ports.py
# User action: archive a port to hide it from active view.
from __future__ import annotations

from flask import redirect, url_for, flash, request
from flask_login import login_required, current_user

from .dashboard_logic import user_bp
from port_logic import archive_port

@user_bp.post("/u/archive")
@login_required
def archive():
    port_id = (request.form.get("port_id") or "").strip()
    if not port_id:
        flash("طلب غير صالح.", "err")
        return redirect(url_for("user.dashboard"))

    result = archive_port(current_user.username, port_id)
    if result.get("ok"):
        flash("تم الأرشفة.", "ok")
    else:
        err = result.get("error")
        if err == "busy":
            flash("يتم معالجة هذا المنفذ حالياً. حاول لاحقاً.", "info")
        elif err == "forbidden":
            flash("هذا المنفذ لا يخص حسابك.", "err")
        elif err == "not_found":
            flash("لم يتم العثور على المنفذ.", "err")
        else:
            flash("تعذر تنفيذ الأرشفة.", "err")

    return redirect(url_for("user.dashboard"))
