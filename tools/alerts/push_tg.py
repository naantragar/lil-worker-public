#!/usr/bin/env python3
"""Deliver a run's push alerts to Telegram — one intercept, one message, one copy-block.

    python3 tools/alerts/push_tg.py knowledge/upstream/alerts/runs/last24h_claude-opus-5_high.json
    python3 tools/alerts/push_tg.py <run.json> --dry          # print, send nothing
    python3 tools/alerts/push_tg.py <run.json> --instance helper

The bot and the recipient come from an instance's own `instance.env`, so this cannot invent a
chat: it sends through `<instance>`'s token to the ids in that instance's ALLOWED_USERS. Default
instance is `helper` (@abdubda_odds_and_ends_bot), the alerting door.

Shape of a message follows the standing raw-intercept rule: a one-line header of ours, then the
intercept VERBATIM inside a fenced block so it carries Telegram's copy button and can be forwarded
as it stands. Nothing of ours inside the block.
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import norm, speech  # noqa: E402 — одна дверь для нормализации речи

REPO = Path(__file__).resolve().parents[2]
CORPUS = "~/wa-monitor/messages.db"
# Джерело видно з першого погляду: наші групи несуть кольорове коло, Патагонія - біле.
# Власник читає червоний канал інакше, коли перехоплення не наше, і має бачити це до тексту.
SRC_NAME = {"Invisible Hand": "Invisible Hand", "AllInARow": "AllInARow",
            "All in a Row": "AllInARow", "PATAGONIA_GP": "Patagonia"}
OURS = {"Invisible Hand", "AllInARow", "All in a Row"}
# Коло = РІВЕНЬ усередині свого потоку. Джерела вже розведені по РІЗНИХ ботах, тому колір більше
# не мусить кричати «це Патагонія» - про це каже сам бот і підпис під заголовком. Усередині
# сірого бота цінно інше: наскільки це важливо. Нові рівні додаються рядком у цю таблицю.
MARK = {"ours":      {"red": "🔴", "yellow": "🟡"},
        "patagonia": {"red": "🟠", "yellow": "⚪"}}
CLASS_RU = {"polon": "ПЛЕН", "vbyto": "ЗАГИБЛИЙ", "vyiavleno": "ВИЯВЛЕНО НАШИХ",
            "pozytsiia": "РОЗКРИТО ПОЗИЦІЮ", "dokumenty": "НАШЕ МАЙНО У НИХ",
            "shturm": "НАЗЕМНИЙ ШТУРМ", "vu": "ВОГНЕВЕ УРАЖЕННЯ", "tekhnika": "ЇХНЯ ТЕХНІКА", "prapor": "ПРАПОР НА ЗАХОПЛЕНІЙ ЗЕМЛІ",
            "vorog_syly": "ЇХНІ УДАРНІ ЗАСОБИ"}


def instance_env(name: str) -> dict:
    p = REPO / "bot" / "instances" / name / "instance.env"
    if not p.exists():
        sys.exit(f"нема {p}")
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def fetch(ids: list[str]) -> dict[str, str]:
    con = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    q = f"SELECT id, coalesce(text,'') FROM messages WHERE id IN ({','.join('?' * len(ids))})"
    out = dict(con.execute(q, ids).fetchall())
    con.close()
    return out


def send(token: str, chat: str, text: str) -> dict:
    data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=data, headers={"Content-Type": "application/json"})
    # Не даємо збою доставки вбити крок: «chat not found» (власник ще не натиснув Start),
    # обрив мережі, ліміт Telegram - усе повертається як провал, і тоді тривога НЕ
    # записується в пам'ять, отже наступний крок спробує її ще раз.
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"{type(e).__name__}: {e}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--instance", default="helper")
    ap.add_argument("--level", default="red", choices=["red", "yellow", "log", "all"])
    ap.add_argument("--source", default="all", choices=["ours", "patagonia", "all"],
                    help="ours = Invisible Hand + AllInARow; patagonia = решта")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--state", default="",
                    help="sqlite зі вже надісланими id; з ним повтори не йдуть")
    ap.add_argument("--seed-only", action="store_true",
                    help="лише записати id у стан, нічого не слати")
    a = ap.parse_args()

    run = json.loads(Path(a.run).read_text(encoding="utf-8"))
    alerts = [x for x in run["alerts"] if a.level == "all" or x.get("level") == a.level]
    # РОЗВЕДЕННЯ ЗА ДЖЕРЕЛОМ. Наш ефір і Патагонія йдуть у РІЗНІ боти, бо увага до них різна:
    # червоне наше - у червоний, жовте наше - у жовтий, червоне з Патагонії - у сірий, а жовте
    # з Патагонії не доставляється взагалі (модель його все одно оцінює й пише в прогін).
    if a.source != "all":
        want_ours = a.source == "ours"
        alerts = [x for x in alerts if (str(x.get("src") or "") in OURS) == want_ours]
    alerts.sort(key=lambda x: x.get("hhmmss", ""))
    if not alerts:
        print("нема чого слати")
        return

    env = instance_env(a.instance)
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chats = [c.strip() for c in env.get("ALLOWED_USERS", "").split(",") if c.strip()]
    if not token or not chats:
        sys.exit(f"в інстансі {a.instance} нема токена або ALLOWED_USERS")

    st = None
    if a.state:
        st = sqlite3.connect(a.state)
        st.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, at TEXT, "
                   "hhmmss TEXT, freq TEXT, class TEXT)")
        cols = {r[1] for r in st.execute("PRAGMA table_info(sent)")}
        if "fp" not in cols:
            st.execute("ALTER TABLE sent ADD COLUMN fp TEXT")
        st.commit()
        if "level" not in cols:
            st.execute("ALTER TABLE sent ADD COLUMN level TEXT")
            st.commit()
        # ПІДВИЩЕННЯ ДОСТАВЛЯЄТЬСЯ. Повільна смуга перечитує ту саму розмову з дописаною ниткою,
        # і те, що спершу було yellow, може виявитись red. Пам'ять тримає РІВЕНЬ, тому повтор
        # того ж рівня мовчить, а підвищення проходить у червоний канал.
        RANK = {"log": 0, "yellow": 1, "red": 2}
        cur = RANK.get(a.level, 1)
        known = {r[0]: RANK.get(r[1] or "yellow", 1)
                 for r in st.execute("SELECT id, level FROM sent")}
        known_fp = {r[0]: RANK.get(r[1] or "yellow", 1)
                    for r in st.execute("SELECT fp, level FROM sent WHERE fp IS NOT NULL")}
        # ОТПЕЧАТОК РЕЧИ, а не только id: один эфир приходит двумя строками (IH и
        # Патагония). Дедуп внутри прогона берёт ту, что уже есть; через 20 минут
        # долетает вторая, становится представителем и ушла бы в бота повторно.
        body = fetch([x["id"] for x in alerts])
        fresh = []
        for x in alerts:
            fp = norm(speech(body.get(x["id"], "")))[:400]
            x["_fp"] = fp
            if known.get(x["id"], -1) >= cur or (fp and known_fp.get(fp, -1) >= cur):
                continue
            known_fp[fp] = cur
            fresh.append(x)
        print(f"нових {len(fresh)} з {len(alerts)} (вже надіслано {len(alerts)-len(fresh)})")
        alerts = fresh
    if a.seed_only:
        if st:
            for x in alerts:
                st.execute("INSERT OR REPLACE INTO sent (id, at, hhmmss, freq, class, fp, level) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (x["id"], time.strftime("%Y-%m-%d %H:%M:%S"), x.get("hhmmss"),
                            x.get("freq"), x.get("class"), x.get("_fp"), x.get("level")))
            st.commit()
        print(f"записано в стан: {len(alerts)}")
        return
    if not alerts:
        return

    texts = fetch([x["id"] for x in alerts])
    for i, x in enumerate(alerts, 1):
        body = texts.get(x["id"], "").strip()
        src = str(x.get("src") or "")
        mark = MARK["ours" if src in OURS else "patagonia"].get(x.get("level"), "⚪")
        src_line = SRC_NAME.get(src, src or "джерело невідоме")
        if x.get("escalated"):
            src_line += f" · у червоний за {x['escalated']}"
        head = (f"{mark} <b>{html.escape(CLASS_RU.get(x.get('class'), str(x.get('class'))))}</b> · "
                f"{html.escape(str(x.get('hhmmss')))} · {html.escape(str(x.get('freq')))}\n"
                f"<b>{html.escape(src_line)}</b>\n"
                f"{html.escape(str(x.get('why') or ''))}")
        msg = (head + "\n\n<pre><code class=\"language-plaintext\">"
               + html.escape(body) + "</code></pre>")
        if len(msg) > 3900:
            msg = msg[:3880] + "…</code></pre>"
        if a.dry:
            print("=" * 70)
            print(msg)
            continue
        delivered = False
        for chat in chats:
            r = send(token, chat, msg)
            delivered = delivered or bool(r.get("ok"))
            print(f"{i}/{len(alerts)} -> {chat}: {'ok' if r.get('ok') else r}")
        if st and delivered:
            st.execute("INSERT OR REPLACE INTO sent (id, at, hhmmss, freq, class, fp, level) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (x['id'], time.strftime('%Y-%m-%d %H:%M:%S'), x.get('hhmmss'),
                        x.get('freq'), x.get('class'), x.get('_fp'), x.get('level')))
            st.commit()
        time.sleep(1.2)


if __name__ == "__main__":
    main()
