"""Shared helpers for locating the withdrawals JSON file.

Supports the legacy filename typo (``withdrawls.json``) while preferring
the correct ``withdrawals.json`` name. The helper returns whichever exists,
defaulting to the correct name for new writes.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def get_withdrawals_file(data_dir: Path = DATA_DIR) -> Path:
    """
    Prefer ``withdrawals.json``; if only ``withdrawls.json`` exists, use it.

    Parameters
    ----------
    data_dir: Path
        Base directory containing the withdrawals files. Defaults to the
        repository-level ``data`` directory.
    """

    correct = data_dir / "withdrawals.json"
    typo = data_dir / "withdrawls.json"

    if correct.exists():
        return correct
    if typo.exists() and not correct.exists():
        return typo
    return correct
