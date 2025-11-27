#!/usr/bin/env python
"""
Minimal integration check to ensure wallet persistence stays stable across
resolves and cleanup actions even without a persistent disk.

Usage:
    python scripts/test_wallet_flow.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _wait_for_file(path: Path, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.05)
    return {}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ["VULNMAP_DATA_DIR"] = str(tmp_path)
        remote_file = tmp_path / "wallet_remote.json"
        os.environ["WALLET_GIST_ID"] = f"file://{remote_file}"
        os.environ["WALLET_GITHUB_TOKEN"] = "local-test-token"
        os.environ["WALLET_GIST_FILENAME"] = "wallet_rayan.json"
        os.environ["DISABLE_KEEP_ALIVE"] = "1"
        os.environ["DISABLE_PORT_SCHEDULER"] = "1"

        import data_paths

        importlib.reload(data_paths)
        from data_paths import get_data_dir

        data_dir = get_data_dir()
        from rayan_wallet import reset_rayan_wallet
        from port_logic import (
            create_port,
            scan_user_assigned,
            resolve_port,
            user_dashboard_view,
        )
        from app import _clear_user_resolved_ports

        gen_dir = data_dir / "ports" / "generated_ports"
        gen_dir.mkdir(parents=True, exist_ok=True)
        for fp in gen_dir.glob("*.json"):
            fp.unlink(missing_ok=True)

        reset_rayan_wallet(data_dir, total_earned=0.0)

        port = create_port("rayan", port_number=4242, reward=7.5)
        scan_user_assigned("rayan")
        resolve_port("rayan", port.id)

        vm = user_dashboard_view("rayan")
        assert abs(vm["wallet"]["total_earned"] - 7.5) < 1e-6, vm["wallet"]

        doc = _wait_for_file(remote_file)
        assert abs(doc.get("total_earned", 0) - 7.5) < 1e-6, doc

        removed = _clear_user_resolved_ports("rayan")
        assert removed >= 1
        vm_after = user_dashboard_view("rayan")
        assert abs(vm_after["wallet"]["total_earned"] - 7.5) < 1e-6, vm_after["wallet"]

        doc2 = _wait_for_file(remote_file)
        assert abs(doc2.get("total_earned", 0) - 7.5) < 1e-6, doc2

        print("Wallet flow test passed.")


if __name__ == "__main__":
    main()
