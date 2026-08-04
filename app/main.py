"""Tailnet Atlas — serves the page and keeps it fed with live data.

A background loop re-renders every few minutes, and what gets served is always
the last SUCCESSFULLY built version. If the Tailscale API is down the page
keeps working and grows a visible "stale" banner instead of breaking.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import render

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

INTERVAL = int(os.environ.get("REFRESH_SECONDS", "300"))
# Floor for live polling: however many browsers poll us, we hit the Tailscale
# API at most once per this many seconds. Clients ask every 10s, so with a
# couple of tabs open this is what actually decides the upstream call rate.
LIVE_TTL = int(os.environ.get("LIVE_TTL_SECONDS", "9"))
app = FastAPI(title="tailnet")

_state = {"html": None, "data": None, "at": None, "error": None,
          "renders": 0, "fails": 0, "live": 0}
_lock = threading.Lock()
# held only while a refresh is in flight, so concurrent viewers coalesce into
# one upstream fetch instead of stampeding
_fetching = threading.Lock()


def refresh() -> bool:
    try:
        data = render.build()
        html = render.render_html(data)
    except Exception as e:
        with _lock:
            _state["error"] = f"{type(e).__name__}: {e}"
            _state["fails"] += 1
        log.warning("render failed: %s", e)
        return False
    with _lock:
        _state.update(html=html, data=data, at=datetime.now(timezone.utc),
                      error=None, renders=_state["renders"] + 1)
    log.info("re-rendered (%d chars)", len(html))
    return True


def age_seconds() -> float:
    with _lock:
        at = _state["at"]
    return 1e9 if at is None else (datetime.now(timezone.utc) - at).total_seconds()


def refresh_if_stale(max_age: float) -> None:
    """Refresh only if the cache is older than max_age AND nobody else is
    already doing it. Never blocks a viewer behind someone else's fetch."""
    if age_seconds() <= max_age:
        return
    if not _fetching.acquire(blocking=False):
        return
    try:
        if age_seconds() > max_age:
            refresh()
    finally:
        _fetching.release()


def loop():
    while True:
        time.sleep(INTERVAL)
        refresh()


BANNER = ('<div style="position:fixed;left:0;right:0;bottom:0;z-index:99;'
          'background:#7a2233;color:#fff;font:13px/1.5 system-ui;'
          'padding:8px 14px;text-align:center">Data is stale — last update '
          '{age} ago ({err})</div>')


@app.get("/api/state")
def api_state():
    """Data-only view for the live poll — a few KB instead of the whole page."""
    refresh_if_stale(LIVE_TTL)
    with _lock:
        data, err, at = _state["data"], _state["error"], _state["at"]
        _state["live"] += 1
    if not data:
        return JSONResponse({"error": err or "not ready"}, status_code=503)
    out = dict(data)
    out["age"] = round(age_seconds(), 1)
    out["error"] = err
    out["at"] = at.isoformat() if at else None
    return JSONResponse(out)


@app.get("/", response_class=HTMLResponse)
def index():
    # A page load should show current data, not whatever the 5-minute loop last
    # left behind — but reuse anything fetched in the last few seconds.
    refresh_if_stale(LIVE_TTL)
    with _lock:
        html, at, err = _state["html"], _state["at"], _state["error"]
    if not html:
        return HTMLResponse(
            "<html><body style='font:15px system-ui;padding:40px'>"
            "Building the tailnet view for the first time…"
            f"{'<br><br>Error: ' + err if err else ''}"
            "<script>setTimeout(()=>location.reload(),5000)</script>"
            "</body></html>", status_code=503)
    if err and at:
        mins = int((datetime.now(timezone.utc) - at).total_seconds() // 60)
        html += BANNER.format(age=f"{mins} min", err=err[:90])
    return HTMLResponse(html)


@app.get("/healthz")
def healthz():
    with _lock:
        at, err = _state["at"], _state["error"]
        n, f = _state["renders"], _state["fails"]
        live = _state["live"]
    return JSONResponse({"ok": bool(_state["html"]), "last_render": at.isoformat() if at else None,
                         "error": err, "renders": n, "failures": f,
                         "interval_seconds": INTERVAL, "live_ttl_seconds": LIVE_TTL,
                         "live_requests": live, "age_seconds": round(age_seconds(), 1)})


@app.post("/api/refresh")
def api_refresh():
    ok = refresh()
    with _lock:
        return JSONResponse({"status": "ok" if ok else "error",
                             "note": "reloaded" if ok else _state["error"]})


def main():
    log.info("initial build…")
    if not refresh():
        log.warning("initial build failed — retrying in the background")
    threading.Thread(target=loop, daemon=True).start()
    log.info("background refresh every %d s", INTERVAL)
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
