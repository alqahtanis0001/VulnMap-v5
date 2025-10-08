# user/dashboard_logic.py
# User blueprint: dashboard view model (SSR) using per-port JSON.
from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from port_logic import user_dashboard_view

# One blueprint for all user actions
user_bp = Blueprint("user", __name__)

@user_bp.get("/u/dashboard")
@login_required
def dashboard():
    vm = user_dashboard_view(current_user.username)
    # Provide both grouped lists and summary figures for template flexibility
    return render_template(
        "user_dashboard.html",
        assigned=vm["assigned"],
        discovered=vm["discovered"],
        resolved=vm["resolved"],
        archived=vm["archived"],
        discovered_count=vm["counts"]["discovered"],
        undiscovered_count=vm["counts"]["assigned"],
        resolved_count=vm["counts"]["resolved"],
        available_balance=vm["wallet"]["available_balance"],
        total_earned=vm["wallet"]["total_earned"],
    )

# Import side-effect routes (scan/resolve/withdraw/archive) so they attach to this blueprint
# These imports must be at the bottom to avoid circular imports.
from . import scan_ports    # noqa: E402,F401
from . import withdraw      # noqa: E402,F401
from . import clear_solved_ports  # noqa: E402,F401
