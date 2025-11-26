# scripts/keep_alive.py
# Keeps a Render/Flask app "warm" by pinging multiple health URLs frequently
# - Silent background daemon thread
# - Prints a line to console on each ping
# - Persists last status to data/keepalive/status.json for admin display
from __future__ import annotations

import os, json, threading, time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error as urlerror

# ---- Module guards ----
_started_lock = threading.Lock()
_started = False

# ---- Defaults ----
DEFAULT_INTERVAL_SEC = 4 * 60  # 4 minutes
REL_STATUS_PATH = Path("keepalive") / "status.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: Path, data) -> None:
    _ensure_dir(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _resolve_ping_urls() -> list[str]:
    """
    Order of preference:
      1) KEEP_ALIVE_URL          (explicit override)
      2) RENDER_EXTERNAL_HOSTNAME (/login on public hostname)
      3) 127.0.0.1:$PORT/login   (local fallback)
    """
    env_url = os.getenv("KEEP_ALIVE_URL", "").strip()
    urls = []
    if env_url:
        urls.append(env_url)

    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if host:
        # Public URL keeps free instances awake
        urls.extend([
            f"https://{host}/",
            f"https://{host}/login",
            f"https://{host}/dashboard"
        ])

    port = os.getenv("PORT", "5000")
    urls.extend([
        f"http://127.0.0.1:{port}/",
        f"http://127.0.0.1:{port}/login",
    ])
    # de-dup and keep order
    seen = set()
    deduped = []
    for u in urls:
        if not u or u in seen:
            continue
        deduped.append(u)
        seen.add(u)
    return deduped or [f"http://127.0.0.1:{port}/"]


def _ping_once(url: str, timeout: float = 10.0) -> tuple[bool, int | None, str | None]:
    try:
        req = request.Request(url, method="GET", headers={"User-Agent": "KeepAlive/1.0"})
        with request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            return (200 <= code < 400, int(code), None)
    except urlerror.HTTPError as e:
        return (False, int(getattr(e, "code", 0) or 0), f"HTTPError: {e}")
    except Exception as e:
        return (False, None, f"Error: {e}")


def _loop(status_file: Path, interval_sec: int) -> None:
    urls = _resolve_ping_urls()
    print(f"[keep_alive] Started. Interval={interval_sec}s, URLs={urls}")
    try:
        _write_json_atomic(status_file, {
            "ts": None,
            "ok": False,
            "http_status": None,
            "url": urls[0] if urls else None,
            "urls": urls,
            "interval_sec": int(interval_sec),
            "error": None,
            "running": True,
            "last_success_url": None,
            "consecutive_failures": 0,
        })
    except Exception:
        pass
    consecutive_failures = 0
    last_success_url = None
    while True:
        for url in urls:
            ok, code, err = _ping_once(url)
            ts = _utcnow_iso()
            if ok:
                consecutive_failures = 0
                last_success_url = url
                print(f"[keep_alive] {ts} -> OK {code} {url}")
            else:
                consecutive_failures += 1
                print(f"[keep_alive] {ts} -> FAIL {code or ''} {err or ''} {url}")

            doc = {
                "ts": ts,
                "ok": bool(ok),
                "http_status": code,
                "url": url,
                "urls": urls,
                "interval_sec": int(interval_sec),
                "error": err if not ok else None,
                "running": True,
                "last_success_url": last_success_url,
                "consecutive_failures": consecutive_failures,
            }
            try:
                _write_json_atomic(status_file, doc)
            except Exception:
                pass
            time.sleep(2)
        jitter = max(30, int(interval_sec)) + int(os.urandom(1)[0] % 15)
        time.sleep(jitter)


def start_keep_alive(data_dir: str | Path, interval_sec: int = DEFAULT_INTERVAL_SEC) -> None:
    """
    Idempotent. Safe to call multiple times; only starts one daemon thread.
    """
    global _started
    if _started:
        return
    with _started_lock:
        if _started:
            return
        root = Path(data_dir).resolve()
        status_file = (root / REL_STATUS_PATH).resolve()
        t = threading.Thread(target=_loop, args=(status_file, int(interval_sec)), daemon=True)
        t.start()
        _started = True


def read_keepalive_status(data_dir: str | Path) -> dict:
    """
    Returns the last recorded status. If none, returns a default.
    """
    status_file = Path(data_dir).resolve() / REL_STATUS_PATH
    doc = _read_json(status_file, {})
    if not isinstance(doc, dict):
        doc = {}
    raw_url = doc.get("url")
    urls = doc.get("urls")
    if isinstance(raw_url, list) and not urls:
        urls = raw_url
        raw_url = raw_url[0] if raw_url else None
    if not isinstance(urls, list):
        urls = []
    # Normalize
    return {
        "ok": bool(doc.get("ok", False)),
        "ts": doc.get("ts"),
        "http_status": doc.get("http_status"),
        "url": raw_url,
        "urls": urls,
        "last_success_url": doc.get("last_success_url"),
        "consecutive_failures": int(doc.get("consecutive_failures") or 0),
        "interval_sec": doc.get("interval_sec"),
        "error": doc.get("error"),
        "running": bool(doc.get("running")),
    }
