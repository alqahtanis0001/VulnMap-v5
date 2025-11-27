"""Optional remote persistence for Rayan's wallet via GitHub Gists.

When running on Render's free tier there is no persistent disk. To retain the
wallet across deploys we allow storing the snapshot inside a private gist. Set
these environment variables to enable it:

- ``WALLET_GITHUB_TOKEN`` (or ``GIST_TOKEN``): PAT with the ``gist`` scope.
- ``WALLET_GIST_ID`` (or ``GIST_ID``): identifier of the target gist.
- Optional ``WALLET_GIST_FILENAME`` (defaults to ``wallet_rayan.json``).

If the env variables are missing the helper silently no-ops and the app falls
back to the local filesystem storage.
"""
from __future__ import annotations

import json
import os
from typing import Optional
from urllib import request, error
from urllib.parse import urlparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError

GIST_ID = os.getenv("WALLET_GIST_ID") or os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("WALLET_GITHUB_TOKEN") or os.getenv("GIST_TOKEN")
GIST_FILENAME = os.getenv("WALLET_GIST_FILENAME", "wallet_rayan.json")
LOCAL_FILE: Optional[Path] = None
API_URL = None
if GIST_ID:
    if GIST_ID.startswith("file://"):
        LOCAL_FILE = Path(urlparse(GIST_ID).path)
    elif GIST_ID.startswith("/") or GIST_ID.startswith("~"):
        LOCAL_FILE = Path(GIST_ID).expanduser()
    else:
        API_URL = f"https://api.github.com/gists/{GIST_ID}"
USER_AGENT = "VulnMapWallet/1.0"
_POOL = ThreadPoolExecutor(max_workers=2)


def has_remote_wallet_store() -> bool:
    return bool(LOCAL_FILE or (API_URL and GITHUB_TOKEN))


def _headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": USER_AGENT,
    }


def fetch_remote_wallet(timeout: float = 10.0) -> Optional[dict]:
    """Return remote wallet dict if configured, else None."""
    if not has_remote_wallet_store():
        return None

    if LOCAL_FILE:
        try:
            if LOCAL_FILE.exists():
                return json.loads(LOCAL_FILE.read_text(encoding="utf-8") or "{}")
        except Exception:
            return None
        return None

    def _fetch():
        req = request.Request(API_URL, method="GET", headers=_headers())
        with request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    future = _POOL.submit(_fetch)
    try:
        data = future.result(timeout=min(timeout, 5.0))
    except TimeoutError:
        future.cancel()
        return None
    except Exception:
        return None

    files = data.get("files", {}) if isinstance(data, dict) else {}
    file_doc = files.get(GIST_FILENAME)
    if not file_doc:
        return None
    content = file_doc.get("content")
    if not content:
        return None
    try:
        return json.loads(content)
    except Exception:
        return None


def persist_remote_wallet(wallet: dict, timeout: float = 10.0) -> bool:
    if not has_remote_wallet_store():
        return False

    if LOCAL_FILE:
        try:
            LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_FILE.write_text(json.dumps(wallet, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def _persist():
        try:
            payload = json.dumps({
                "files": {
                    GIST_FILENAME: {
                        "content": json.dumps(wallet, ensure_ascii=False, indent=2)
                    }
                }
            }).encode("utf-8")
            req = request.Request(API_URL, method="PATCH", headers={
                **_headers(),
                "Content-Type": "application/json",
            }, data=payload)
            with request.urlopen(req, timeout=timeout) as resp:
                resp.read()
        except Exception:
            pass

    _POOL.submit(_persist)
    return True
