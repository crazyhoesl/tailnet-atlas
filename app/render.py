"""Builds the live Tailnet Atlas page from the Tailscale API.

Two things are derived rather than hard-coded, because hard-coding them is
exactly what broke the first version:

* EDGES come from the actual grants in the policy file. The first version
  listed node ids by hand; as soon as ids are generated from live data, none
  of them match and the graph silently loses every line — no error, just a
  still picture.
* SITES/positions come from the address a device was last seen on, so a new
  device lands somewhere sensible instead of disappearing.

Everything site-specific lives in config.toml, not in here.
"""
from __future__ import annotations

import html
import ipaddress
import json
import logging
import os
import re
import tomllib
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("render")

API = "https://api.tailscale.com/api/v2"
CID = os.environ.get("TS_OAUTH_CLIENT_ID", "")
SECRET = os.environ.get("TS_OAUTH_SECRET", "")
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
TEMPLATE = os.environ.get("TEMPLATE_PATH", "/app/template.html")
CONFIG = os.environ.get("CONFIG_PATH", "/app/config.toml")
REFRESH = int(os.environ.get("REFRESH_SECONDS", "300"))

# Two site ids carry meaning in the code and must exist in the config:
#   dock   — everything left inside 172.16/12, i.e. container bridges
#   mobile — the fallback for anything that matched nothing
RESERVED = ("dock", "mobile")

DOCKER_RE = re.compile(r"^172\.(1[6-9]|2[0-9]|3[01])\.")


def load_config(path: str = CONFIG) -> dict:
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except FileNotFoundError:
        raise SystemExit(
            f"no config at {path}\n"
            "  cp config.example.toml config.toml   # then edit it\n"
            "config.toml is mounted into the container by compose.yaml.")
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path} is not valid TOML: {e}")
    sites = cfg.get("site") or []
    ids = [s["id"] for s in sites]
    missing = [r for r in RESERVED if r not in ids]
    if missing:
        raise SystemExit(f"config.toml: missing required site id(s): {missing}")
    if len(set(ids)) != len(ids):
        raise SystemExit("config.toml: duplicate site ids")
    return cfg


_cfg = load_config()

TAILNET = _cfg.get("tailnet", "my tailnet")

SITE_ORDER = [s["id"] for s in _cfg["site"]]
SITE_META = {
    s["id"]: {"label": s["label"], "net": s.get("net", ""),
              "fx": float(s.get("fx", .5)), "fy": float(s.get("fy", .5)),
              "tint": s.get("tint", "127,150,200")}
    for s in _cfg["site"]
}

# Address prefix -> site. Order matters and is taken from the config file:
# the first match wins, which is how a hotspot range can be claimed before the
# generic container-bridge rule gets to look at it.
SITE_RULES = [(s["match"], s["id"]) for s in _cfg["site"] if s.get("match")]

# Devices that always count as roaming whatever address they report — a cloud
# VM has no LAN that would place it anywhere meaningful.
ALWAYS_MOBILE = set(_cfg.get("pinned_roaming") or [])


def _token() -> str:
    r = httpx.post(f"{API}/oauth/token", auth=(CID, SECRET),
                   data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch() -> tuple[list, str]:
    h = {"Authorization": f"Bearer {_token()}"}
    d = httpx.get(f"{API}/tailnet/-/devices?fields=all", headers=h, timeout=45)
    d.raise_for_status()
    a = httpx.get(f"{API}/tailnet/-/acl", headers=h, timeout=45)
    a.raise_for_status()
    return d.json().get("devices", []), a.text


# --------------------------------------------------------------- HuJSON
def strip_hujson(text: str) -> str:
    """HuJSON -> JSON. Character-wise, not regex: the policy has comments at
    the END of lines, and a naive `//` rule would also destroy https:// URLs
    inside strings. So string/escape state is tracked."""
    out, i, n = [], 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def parse_acl(text: str) -> dict:
    try:
        return json.loads(strip_hujson(text))
    except json.JSONDecodeError as e:
        log.warning("policy not parseable: %s", e)
        return {}


# --------------------------------------------------------------- devices
def is_own_interface(ip: str) -> bool:
    """True if the device HOSTS this network rather than having joined it.

    Whoever hands out a network holds the gateway address in it. A travel
    router creates its own LAN and sits on .1 there; container bridges on a
    Docker host are the same story. Those addresses travel with the device and
    say nothing about where it is — only the remaining one, the address it was
    GIVEN by the network it plugged into, is its uplink.

    Deliberately NOT using the device's advertised routes for this: a subnet
    router that merely JOINED the network it announces holds a normal host
    address in it, and that test would wrongly strip its real address. The
    gateway convention separates "I created this network" from "I joined this
    network" exactly.
    """
    return ip.endswith(".1")


def lan_and_site(dev: dict) -> tuple[str, str, bool]:
    """-> (address to show, site, whether that address is an uplink/WAN)."""
    eps = ((dev.get("clientConnectivity") or {}).get("endpoints") or [])
    ips = []
    for e in eps:
        ip = e.rsplit(":", 1)[0].strip("[]")
        if ":" in ip:
            continue
        if ip.startswith(("192.168.", "172.", "10.")):
            ips.append(ip)
    # Locate the device by its uplink, not by the networks it serves. Fall back
    # to the full list if it has nothing else, so a device is never left blank.
    pool = [ip for ip in ips if not is_own_interface(ip)] or ips

    # Label it an uplink only for actual subnet routers whose address sits
    # OUTSIDE the network they announce — that is the travel-router case. Not
    # for a box that announces the LAN it lives in, and not for machines that
    # merely happen to hold Docker or VM bridge gateways.
    served = [r for r in (dev.get("advertisedRoutes") or [])
              if r not in ("0.0.0.0/0", "::/0") and ":" not in r]
    def _outside(ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return all(a not in ipaddress.ip_network(r, strict=False) for r in served)
    def result(ip: str, site: str) -> tuple[str, str, bool]:
        return ip, site, bool(served) and _outside(ip)

    for prefix, site in SITE_RULES:
        for ip in pool:
            if ip.startswith(prefix):
                return result(ip, site)
    for ip in pool:
        if DOCKER_RE.match(ip):
            return result(ip, "dock")
    return result(pool[0], "mobile") if pool else ("—", "mobile", False)


def role_of(tags: list[str]) -> str:
    t = set(tags or [])
    if "tag:home" in t:
        return "home"
    if "tag:client-full" in t:
        return "full"
    if "tag:client" in t:
        return "client"
    if "tag:server" in t:
        return "server"
    if "tag:exit" in t:
        return "exit"
    if "tag:ha-access" in t:
        return "proxy"
    return "client"


def short_id(name: str, used: set) -> str:
    base = re.sub(r"[^a-z0-9]", "", name.lower())[:10] or "n"
    cand, i = base, 1
    while cand in used:
        i += 1
        cand = f"{base}{i}"
    used.add(cand)
    return cand


def build_devices(devs: list) -> list[dict]:
    out, used = [], set()
    for d in devs:
        name = d["name"].split(".")[0]
        tags = d.get("tags") or []
        lan, site, is_wan = lan_and_site(d)
        if name in ALWAYS_MOBILE:
            site = "mobile"
        enabled = d.get("enabledRoutes") or []
        routes = [r for r in enabled if r not in ("0.0.0.0/0", "::/0")]
        out.append({
            "id": short_id(name, used), "name": name, "tags": tags,
            # full MagicDNS name: the page probes https://<fqdn>/ from the
            # viewer's browser to decide whether the node is clickable
            "fqdn": (d.get("name") or "").rstrip("."),
            "ip": (d.get("addresses") or ["—"])[0],
            "lan": lan, "lanKind": "uplink" if is_wan else "lan", "site": site,
            # empty user = joined on a tagged auth key, owned by the tag.
            # ownerKind is filled in by build() once the user list is known.
            "owner": (d.get("user") or TAG_OWNED), "ownerKind": "self",
            "siteLabel": SITE_META[site]["label"],
            "os": (d.get("os") or "").lower(),
            "role": role_of(tags),
            "exit": "0.0.0.0/0" in enabled,
            "subnet": bool(routes), "routes": routes,
            "online": bool(d.get("connectedToControl")),
            "update": bool(d.get("updateAvailable")),
            "user": d.get("user", ""),
        })
    out.sort(key=lambda x: x["name"])
    return out


def build_sites(devs: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for d in devs:
        counts[d["site"]] = counts.get(d["site"], 0) + 1
    sites = []
    for sid in SITE_ORDER:
        n = counts.get(sid, 0)
        if not n:
            continue
        m = SITE_META[sid]
        # radius grows with population but stays clear of the neighbours
        fr = min(0.28, max(0.085, 0.075 + 0.024 * n))
        sites.append({"id": sid, "label": m["label"], "net": m["net"],
                      "fx": m["fx"], "fy": m["fy"], "fr": round(fr, 3),
                      "tint": m["tint"], "count": n})
    return sites


# --------------------------------------------------------------- edges
def expand(target: str, devs: list[dict]) -> list[dict]:
    """Which devices does a grant source/target refer to?"""
    if target.startswith("tag:"):
        return [d for d in devs if target in d["tags"]]
    if target == "autogroup:internet":
        return [d for d in devs if d["exit"]]          # leaves the net via exit nodes
    if re.match(r"^\d+\.\d+\.\d+\.\d+/\d+$", target):
        return [d for d in devs if target in d["routes"]]   # the announcing router
    return []


KIND = {"tag:client-full": "full", "tag:ha-access": "ha"}


def build_edges(acl: dict, devs: list[dict]) -> tuple[list[dict], list[int]]:
    edges, per_grant = [], []
    seen = set()
    for g in (acl.get("grants") or []):
        srcs = g.get("src") or []
        dsts = g.get("dst") or []
        kind = "peer"
        for s in srcs:
            kind = KIND.get(s, kind)
        n = 0
        for s in srcs:
            for a in expand(s, devs):
                for t in dsts:
                    for b in expand(t, devs):
                        if a["id"] == b["id"]:
                            continue
                        key = (a["id"], b["id"], kind)
                        if key in seen:
                            continue
                        seen.add(key)
                        # same tag on both sides = mutual reachability
                        bidir = bool(set(srcs) & set(dsts))
                        edges.append({"a": a["id"], "b": b["id"], "kind": kind,
                                      "faint": len(dsts) > 3,
                                      **({"bidir": True} if bidir else {})})
                        n += 1
        per_grant.append(n)
    return edges, per_grant


# --------------------------------------------------------------- html bits
def chip(v: str) -> str:
    k = v.replace("tag:", "")
    cls = "full" if k == "client-full" else k
    return f'<span class="chip {html.escape(cls)}">{html.escape(k)}</span>'


def device_rows(devs: list[dict]) -> str:
    rows = []
    for d in devs:
        chips = "".join(chip(t) for t in sorted(d["tags"])) or \
            '<span class="note">untagged</span>'
        if d["routes"]:
            chips += f'<span class="note">{html.escape(" ".join(d["routes"]))}</span>'
        # name the owner only when it is NOT the tailnet owner — otherwise the
        # column repeats the same login on almost every row. ownerKind is set
        # in build_users() from the API's role, never from a hardcoded login.
        if d["ownerKind"] == "other":
            chips += f'<span class="note">user: {html.escape(d["owner"].split("@")[0])}</span>'
        if d["update"]:
            chips += '<span class="note">update available</span>'
        off = "" if d["online"] else ' <span class="off">offline</span>'
        rows.append(
            f'<tr><td class="name">{html.escape(d["name"])}{off}</td>'
            f'<td class="ip">{html.escape(d["ip"])}</td>'
            f'<td class="ip">{html.escape(d["lan"])} '
            f'<span class="note">·{d["site"]}</span></td>'
            f'<td class="ip">{html.escape(d["os"])}</td>'
            f'<td><div class="chips">{chips}</div></td></tr>')
    return "\n".join(rows)


def grant_rows(acl: dict, counts: list[int]) -> str:
    rows = []
    for i, g in enumerate((acl.get("grants") or []), 1):
        src = " ".join(chip(x) for x in (g.get("src") or []))
        dst = ' <span class="to">·</span> '.join(
            chip(x) if x.startswith("tag:") else
            f'<span class="ip">{html.escape(x)}</span>' for x in (g.get("dst") or []))
        ips = g.get("ip") or []
        ports = ('<span class="port all">any</span>' if ips == ["*"] else
                 " ".join(f'<span class="port">{html.escape(p)}</span>' for p in ips))
        n = counts[i - 1] if i - 1 < len(counts) else 0
        rows.append(f'<tr><td class="ip">{i}</td><td>{src}</td>'
                    f'<td class="flow">{dst}</td><td>{ports}</td>'
                    f'<td class="ip">{n}</td></tr>')
    return "\n".join(rows)


TAG_OWNED = "__tagged__"


def fetch_users() -> list[dict]:
    """Tailnet users. Failing this must not take the page down — ownership is
    an extra, the map works without it."""
    try:
        r = httpx.get(f"{API}/tailnet/-/users",
                      headers={"Authorization": f"Bearer {_token()}"}, timeout=30)
        r.raise_for_status()
        return r.json().get("users") or []
    except Exception as e:
        log.warning("users not available: %s", e)
        return []


def build_users(devs: list[dict]) -> list[dict]:
    """Owners with the devices attributed to them.

    Devices that joined on a tagged auth key have no user at all — they belong
    to their tag. That is not an error and it is worth showing, so they get
    their own pseudo-owner rather than being dropped.

    Also stamps each device with ownerKind: whose it is relative to the tailnet
    owner. Which login that is comes from the API (role == "owner"), not from a
    hardcoded name.
    """
    api = {u.get("loginName"): u for u in fetch_users()}
    primary = next((login for login, u in api.items()
                    if u.get("role") == "owner"), None)
    for d in devs:
        d["ownerKind"] = ("tag" if d["owner"] == TAG_OWNED else
                          ("self" if d["owner"] == primary else "other"))
    order = [u for u in api] + [TAG_OWNED]
    out = []
    for login in order:
        mine = [d for d in devs if d["owner"] == login]
        if not mine and login != TAG_OWNED:
            continue
        if not mine:
            continue
        u = api.get(login) or {}
        sites = sorted({d["siteLabel"] for d in mine})
        out.append({
            "login": login,
            "name": ("tag-owned" if login == TAG_OWNED
                     else (u.get("displayName") or login.split("@")[0])),
            "role": "—" if login == TAG_OWNED else (u.get("role") or "member"),
            "status": "—" if login == TAG_OWNED else (u.get("status") or ""),
            "devices": len(mine),
            "online": sum(1 for d in mine if d["online"]),
            "names": sorted(d["name"] for d in mine),
            "sites": sites,
        })
    out.sort(key=lambda x: (x["login"] == TAG_OWNED, -x["devices"]))
    return out


def user_rows(users: list[dict]) -> str:
    rows = []
    for u in users:
        tag = ("tag-owned" if u["login"] == TAG_OWNED else
               ("owner" if u["role"] == "owner" else "member"))
        who = (f'<span class="name">{html.escape(u["name"])}</span>'
               if u["login"] == TAG_OWNED else
               f'<span class="name">{html.escape(u["name"])}</span>'
               f'<div class="note">{html.escape(u["login"])}</div>')
        devs = " ".join(f'<span class="chip">{html.escape(n)}</span>'
                        for n in u["names"])
        rows.append(
            f'<tr><td>{who}</td>'
            f'<td><span class="chip {"home" if tag == "owner" else "client"}">'
            f'{tag}</span></td>'
            f'<td class="ip">{u["online"]}/{u["devices"]}</td>'
            # one per line: the site labels contain "·" themselves
            f'<td class="note nw">'
            + "<br>".join(html.escape(s) for s in u["sites"]) + '</td>'
            f'<td><div class="chips">{devs}</div></td></tr>')
    return "\n".join(rows)


def rule_rows() -> str:
    """Document the placement logic — generated FROM the constants that drive
    it, so the page cannot drift away from the code."""
    def net(x):
        return f'<span class="ip">{html.escape(x)}</span>'

    steps: list[tuple[str, str, str]] = [
        ("Collect",
         "Take every private address the device was last seen on "
         f"({net('10.x')}, {net('172.16-31.x')}, {net('192.168.x')}).",
         "Tailscale reports all interfaces, not just the useful one."),
        ("Drop self-hosted",
         f"Ignore any address ending in {net('.1')} — the gateway of a network "
         "the device hands out itself.",
         "Travel routers carry their own LAN everywhere; it says nothing "
         "about location. Same for Docker bridges."),
    ]
    # one per line — the site labels contain "·" themselves, so joining the
    # rules with the same character made the list unreadable
    place = "<br>".join(f"{net(p + 'x')} &rarr; {SITE_META[s]['label']}"
                        for p, s in SITE_RULES)
    steps.append(("Match a place", place,
                  "Only networks bound to a physical location count. "
                  "The hotspot range is listed so it is not mistaken for a "
                  "Docker bridge — both sit inside 172.16/12."))
    steps.append(("Docker",
                  f"Anything left inside {net('172.16/12')} joins the "
                  f"{SITE_META['dock']['label']} cluster.",
                  "Containers share the host's netns and have no LAN of "
                  "their own."))
    steps.append(("Otherwise",
                  f"{SITE_META['mobile']['label']}.",
                  "Mobile data, a hotspot or an unknown network — the device "
                  "is not at any known site."))
    if ALWAYS_MOBILE:
        pinned = ", ".join(f'<span class="name">{html.escape(n)}</span>'
                           for n in sorted(ALWAYS_MOBILE))
        steps.append(("Pinned", f"{pinned} always counts as roaming.",
                      "A datacenter VM has no LAN that would place it "
                      "anywhere meaningful."))
    return "\n".join(
        f'<tr><td class="name">{i}. {t}</td><td>{r}</td>'
        f'<td class="note">{w}</td></tr>'
        for i, (t, r, w) in enumerate(steps, 1))


def site_rows(sites: list[dict], devs: list[dict]) -> str:
    rows = []
    for s in sites:
        members = [d for d in devs if d["site"] == s["id"]]
        roles = sorted({d["role"] for d in members})
        rows.append(
            f'<tr><td class="name">{html.escape(s["label"])}</td>'
            f'<td class="ip">{html.escape(s["net"])}</td>'
            f'<td class="ip">{len(members)}</td>'
            f'<td><div class="chips">'
            + "".join(f'<span class="chip {r}">{r}</span>' for r in roles)
            + '</div></td></tr>')
    return "\n".join(rows)


RULE_NOTE = ("Rules are rendered from the same constants the placement code "
             "uses, so this table cannot fall out of sync. A device that is "
             "offline keeps its last known address, and therefore its last "
             "known site, until it checks in again.")


# --------------------------------------------------------------- render
def build() -> dict:
    """Everything the page needs, as data.

    Kept separate from the HTML so the live poll can ship a few KB of JSON
    instead of re-sending the whole document every ten seconds.
    """
    devs_raw, acl_text = fetch()
    devs = build_devices(devs_raw)
    acl = parse_acl(acl_text)
    sites = build_sites(devs)
    edges, counts = build_edges(acl, devs)
    users = build_users(devs)

    node_js = [{"id": d["id"], "name": d["name"], "role": d["role"],
                "site": d["site"], "siteLabel": d["siteLabel"], "ip": d["ip"],
                "lan": d["lan"], "lanKind": d["lanKind"],
                "loc": d["site"], "os": d["os"], "owner": d["owner"],
                "ownerKind": d["ownerKind"],
                "tags": d["tags"], "routes": d["routes"], "online": d["online"],
                "fqdn": d["fqdn"]}
               for d in devs]

    return {
        "nodes": node_js, "edges": edges, "sites": sites,
        "kpi": {"devices": len(devs),
                "online": sum(1 for d in devs if d["online"]),
                "grants": len(acl.get("grants") or []),
                "sites": len(sites)},
        "rows": {"devices": device_rows(devs),
                 "grants": grant_rows(acl, counts),
                 "sites": site_rows(sites, devs),
                 "users": user_rows(users)},
        "users": users,
        "stamp": datetime.now(TZ).strftime("%d %b %Y · %H:%M:%S"),
    }


def render_html(data: dict) -> str:
    mins = REFRESH // 60
    tpl = open(TEMPLATE).read()
    return (tpl
            .replace("__NODES__", json.dumps(data["nodes"], ensure_ascii=False))
            .replace("__EDGES__", json.dumps(data["edges"]))
            .replace("__SITES__", json.dumps(data["sites"], ensure_ascii=False))
            .replace("__DEVICE_ROWS__", data["rows"]["devices"])
            .replace("__GRANT_ROWS__", data["rows"]["grants"])
            .replace("__SITE_ROWS__", data["rows"]["sites"])
            .replace("__USER_ROWS__", data["rows"]["users"])
            .replace("__RULE_ROWS__", rule_rows())
            .replace("__RULE_NOTE__", RULE_NOTE)
            .replace("__N_DEVICES__", str(data["kpi"]["devices"]))
            .replace("__N_ONLINE__", str(data["kpi"]["online"]))
            .replace("__N_GRANTS__", str(data["kpi"]["grants"]))
            .replace("__N_SITES__", str(data["kpi"]["sites"]))
            .replace("__N_USERS__", str(len(data["users"])))
            .replace("__REFRESH__", f"{mins} min" if mins else f"{REFRESH}s")
            .replace("__TAILNET__", TAILNET)
            .replace("__TAILNET_SHORT__", TAILNET.split(" · ")[0])
            .replace("__STAMP__", data["stamp"]))


def render() -> str:
    return render_html(build())


def snapshot() -> dict:
    devs_raw, acl_text = fetch()
    devs = build_devices(devs_raw)
    acl = parse_acl(acl_text)
    edges, _ = build_edges(acl, devs)
    return {"devices": len(devs), "online": sum(1 for d in devs if d["online"]),
            "grants": len(acl.get("grants") or []), "edges": len(edges),
            "sites": len(build_sites(devs))}
