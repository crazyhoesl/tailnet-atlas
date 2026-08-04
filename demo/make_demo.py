"""Builds a demo copy of the Atlas from invented data.

Deliberately NOT a mock-up: fictional devices are pushed through the real
render.py, so what you see is the actual product with different input — same
site detection, same edges derived from grants, same tables.

    python3 demo/make_demo.py          # -> demo/out/index.html + state.json

Nothing here talks to the Tailscale API, so it needs no credentials.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# render.py imports httpx for the live API. The demo replaces every call that
# would use it, and installing it just to import the module is silly.
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

import os  # noqa: E402

os.environ.setdefault("CONFIG_PATH", str(pathlib.Path(__file__).parent / "demo.toml"))
os.environ.setdefault("TEMPLATE_PATH", str(ROOT / "app" / "template.html"))

import render  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent / "out"

OWNER, MATE, COLLEAGUE = "nadia@github", "theo@github", "mila@github"

# name, tags, endpoints, advertised routes, os, online, owner
# ("" as owner = joined on a tagged auth key, so it has no user at all)
D = [
    ("hub", ["tag:home", "tag:server", "tag:exit", "tag:subnet"],
     ["172.30.0.1", "192.168.10.20"], ["0.0.0.0/0", "192.168.10.0/24"], "linux", True, OWNER),
    ("crate", ["tag:server"],
     ["172.17.0.1", "172.18.0.1", "192.168.10.30"], [], "linux", True, OWNER),
    ("thinkpad-x1", ["tag:client-full"], ["192.168.10.41"], [], "linux", True, OWNER),
    ("nadia-pixel", ["tag:client-full"], ["10.66.4.9", "192.168.10.42"], [], "android", True, OWNER),
    ("living-room-tv", ["tag:client"], ["192.168.10.51"], [], "android", True, OWNER),
    ("kitchen-tablet", ["tag:client", "tag:ha-access"], ["192.168.10.52"], [], "android", False, OWNER),
    ("theo-iphone", ["tag:client", "tag:ha-access"], ["192.168.10.53"], [], "iOS", True, MATE),
    ("theo-macbook", ["tag:client-full"], ["192.168.10.54"], [], "macOS", True, MATE),

    ("office-nuc", ["tag:server", "tag:subnet", "tag:exit"],
     ["10.20.30.2"], ["0.0.0.0/0", "10.20.30.0/24"], "linux", True, COLLEAGUE),
    ("plotter", ["tag:client"], ["10.20.30.31"], [], "linux", True, COLLEAGUE),
    ("mila-laptop", ["tag:client-full"], ["10.20.30.44"], [], "windows", True, COLLEAGUE),

    ("photos", ["tag:server"], ["172.19.0.2"], [], "linux", True, OWNER),
    ("notes", ["tag:server"], ["172.19.0.3"], [], "linux", True, OWNER),
    ("metrics", ["tag:server"], ["172.21.0.2"], [], "linux", True, OWNER),
    ("backups", ["tag:server"], ["172.22.0.2"], [], "linux", True, OWNER),
    ("recipes", ["tag:server", "tag:ha-access"], ["172.23.0.2"], [], "linux", True, ""),
    ("doorbell-relay", ["tag:ha-access"], ["172.24.0.3"], [], "linux", True, ""),
    ("atlas", ["tag:server"], ["172.25.0.3"], [], "linux", True, ""),
    ("wiki", ["tag:server"], ["172.26.0.2"], [], "linux", True, OWNER),

    # roaming: travel routers hand out their own LAN and hold .1 in it, so they
    # are placed by the address they were GIVEN — the second one in each list
    ("van-router", ["tag:exit", "tag:server", "tag:subnet"],
     ["192.168.8.1", "10.44.2.87"], ["192.168.8.0/24"], "linux", True, OWNER),
    ("trip-router", ["tag:exit", "tag:server", "tag:subnet"],
     ["192.168.9.1", "172.20.10.6"], ["192.168.9.0/24"], "linux", False, OWNER),
    ("mila-ipad", ["tag:client", "tag:ha-access"], ["172.20.10.4"], [], "iOS", False, COLLEAGUE),
    ("cloud-exit", ["tag:exit"], ["10.0.19.7"], ["0.0.0.0/0"], "linux", True, OWNER),
]

ACL = """
{
  // demo policy — same shape as a real one, invented contents
  "tagOwners": {
    "tag:home": ["autogroup:owner"], "tag:server": ["autogroup:owner"],
    "tag:exit": ["autogroup:owner"], "tag:subnet": ["autogroup:owner"],
    "tag:client": ["autogroup:owner"], "tag:client-full": ["autogroup:owner"],
    "tag:ha-access": ["autogroup:owner"]
  },
  "grants": [
    { "src": ["tag:client-full"], "dst": ["tag:server", "tag:home"], "ip": ["*"] },
    { "src": ["tag:client"], "dst": ["tag:home"], "ip": ["443", "8123"] },
    { "src": ["tag:ha-access"], "dst": ["tag:home"], "ip": ["8123"] },
    // deliberately narrow: a server-to-server grant draws n*(n-1) lines and
    // turns the container cluster into a solid blob
    { "src": ["tag:server"], "dst": ["tag:home"], "ip": ["514"] }
  ]
}
"""

USERS = [
    {"loginName": OWNER, "displayName": "nadia", "role": "owner", "status": "active"},
    {"loginName": MATE, "displayName": "theo", "role": "member", "status": "active"},
    {"loginName": COLLEAGUE, "displayName": "mila", "role": "member", "status": "active"},
]


def devices() -> list[dict]:
    out = []
    for i, (name, tags, eps, routes, os_, online, owner) in enumerate(D):
        out.append({
            "name": f"{name}.tin-otter.ts.net",
            "addresses": [f"100.{80 + i // 8}.{11 + i * 3}.{7 + i}"],
            "tags": tags, "os": os_, "user": owner,
            "connectedToControl": online,
            "updateAvailable": i % 9 == 4,
            "advertisedRoutes": routes, "enabledRoutes": routes,
            "clientConnectivity": {"endpoints": [f"{e}:41641" for e in eps]},
        })
    return out


def main() -> None:
    render.fetch = lambda: (devices(), ACL)
    render.fetch_users = lambda: USERS

    data = render.build()
    page = render.render_html(data)

    OUT.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / "state.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # A second copy that works straight off disk. Opened as a file there is no
    # /api/state, and the live poll would paint a red error badge over the map;
    # liveStop must be SET rather than merely "not started", or the
    # visibilitychange handler restarts polling on the first tab switch.
    marker = "if (!document.hidden) { startLive(); poll(); }"
    if marker not in page:
        raise SystemExit("live bootstrap not found — standalone build would poll and fail")
    (OUT / "atlas-demo.html").write_text(
        page.replace(marker, "liveStop = true; setLive('\\u00b7 demo snapshot', 'idle');"),
        encoding="utf-8")

    print("sites :", " ".join(f'{s["id"]}={s["count"]}' for s in data["sites"]))
    print("nodes :", len(data["nodes"]), " edges:", len(data["edges"]),
          " owners:", ", ".join(f'{u["name"]}={u["devices"]}' for u in data["users"]))
    print("wrote :", OUT)


if __name__ == "__main__":
    main()
