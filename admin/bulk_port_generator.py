# admin/bulk_port_generator.py
# Admin action: bulk-assign ports to a user (append-only; per-port files).
from __future__ import annotations

import random

from flask import request, redirect, url_for, flash
from flask_login import login_required, current_user

from .dashboard_logic import admin_bp
from port_logic import create_port


def _require_admin():
    if not getattr(current_user, "is_admin", False):
        from flask import abort
        abort(403)


@admin_bp.post("/admin/assign")
@login_required
def assign_ports():
    _require_admin()

    username = (request.form.get("username") or "").strip().lower()

    try:
        count = int(request.form.get("count", 0))
    except (TypeError, ValueError):
        count = 0

    try:
        reward_min = float(request.form.get("reward_min", 1.10))
        reward_max = float(request.form.get("reward_max", 4.25))
    except (TypeError, ValueError):
        reward_min, reward_max = 1.10, 4.25

    try:
        delay_min = int(request.form.get("delay_min", 0))
        delay_max = int(request.form.get("delay_max", 7))
    except (TypeError, ValueError):
        delay_min, delay_max = 0, 7

    if not username or count <= 0:
        flash("Username and a positive count are required.", "err")
        return redirect(url_for("admin.dashboard"))

    for _ in range(count):
        port_number = random.randint(1024, 9999)
        reward = round(random.uniform(reward_min, reward_max), 2)
        resolve_delay_sec = random.randint(delay_min, delay_max)
        create_port(username, port_number, reward, resolve_delay_sec)

    flash(f"Assigned {count} ports to {username}.", "ok")
    return redirect(url_for("admin.dashboard"))
