#!/usr/bin/env python3
"""Tiny stdlib-only Matrix client-API helper used by matrix/setup.sh.

Exists so the installer can do the fiddly parts FOR the user — obtain an access token from a
password, discover which rooms the bot was invited to, accept those invites, and send a hello
message — instead of telling them to dig through Element's settings for a token and an internal
room id. Stdlib only: it must run before the venv has any dependencies installed.

Subcommands (all print JSON on stdout, human errors on stderr, non-zero exit on failure):
    login    <homeserver> <user_or_mxid> <password>
    whoami   <homeserver> <token>
    rooms    <homeserver> <token>          # joined + invited, with names
    join     <homeserver> <token> <room_id>
    send     <homeserver> <token> <room_id> <text>
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30


def _call(hs: str, path: str, token: str | None = None, body: dict | None = None,
          method: str | None = None) -> dict:
    url = hs.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            err = json.loads(raw)
            msg = err.get("error") or raw
        except Exception:
            msg = raw
        die(f"{e.code} from {path}: {msg}")
    except urllib.error.URLError as e:
        die(f"cannot reach {hs}: {e.reason}")
    return {}


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def cmd_login(hs: str, user: str, password: str) -> dict:
    # Accept either "@bot:server" or a bare localpart — the server resolves both.
    ident = {"type": "m.id.user", "user": user}
    res = _call(hs, "/_matrix/client/v3/login", body={
        "type": "m.login.password",
        "identifier": ident,
        "password": password,
        "initial_device_display_name": "lil_worker matrix bridge",
    })
    return {"user_id": res.get("user_id"), "access_token": res.get("access_token"),
            "device_id": res.get("device_id")}


def cmd_whoami(hs: str, token: str) -> dict:
    return _call(hs, "/_matrix/client/v3/account/whoami", token=token)


def cmd_rooms(hs: str, token: str) -> dict:
    """Joined and invited rooms with display names, via one immediate sync."""
    flt = json.dumps({
        "room": {
            "timeline": {"limit": 1},
            "state": {"types": ["m.room.name", "m.room.encryption"]},
        }
    })
    q = urllib.parse.urlencode({"timeout": "0", "filter": flt})
    res = _call(hs, f"/_matrix/client/v3/sync?{q}", token=token)
    rooms = res.get("rooms") or {}

    def name_and_crypto(state_events: list) -> tuple[str, bool]:
        name, enc = "", False
        for ev in state_events or []:
            if ev.get("type") == "m.room.name":
                name = (ev.get("content") or {}).get("name") or name
            elif ev.get("type") == "m.room.encryption":
                enc = True
        return name, enc

    out = []
    for rid, data in (rooms.get("join") or {}).items():
        n, enc = name_and_crypto(((data.get("state") or {}).get("events")) or [])
        out.append({"room_id": rid, "name": n, "membership": "join", "encrypted": enc})
    for rid, data in (rooms.get("invite") or {}).items():
        n, enc = name_and_crypto(((data.get("invite_state") or {}).get("events")) or [])
        out.append({"room_id": rid, "name": n, "membership": "invite", "encrypted": enc})
    return {"rooms": out}


def cmd_join(hs: str, token: str, room_id: str) -> dict:
    return _call(hs, "/_matrix/client/v3/join/" + urllib.parse.quote(room_id), token=token, body={})


def cmd_send(hs: str, token: str, room_id: str, text: str) -> dict:
    path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
            f"/send/m.room.message/{urllib.parse.quote(str(abs(hash(text))))}")
    return _call(hs, path, token=token, body={"msgtype": "m.text", "body": text}, method="PUT")


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        die(__doc__ or "usage: setup_api.py <command> …")
    cmd, args = argv[1], argv[2:]
    table = {
        "login": (cmd_login, 3),
        "whoami": (cmd_whoami, 2),
        "rooms": (cmd_rooms, 2),
        "join": (cmd_join, 3),
        "send": (cmd_send, 4),
    }
    if cmd not in table:
        die(f"unknown command: {cmd}")
    fn, n = table[cmd]
    if len(args) != n:
        die(f"{cmd} expects {n} argument(s), got {len(args)}")
    print(json.dumps(fn(*args), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
