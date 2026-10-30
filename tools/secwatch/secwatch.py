#!/usr/bin/env python3
"""Security watcher: wakes from cron, checks a security item, and messages the owner ONLY on a change.

Built 30.10.2026 for CVE-2026-98432 (kernel TCP SYN + IP options use-after-free). The owner wanted
something that checks by itself and tells him when the situation moves, with advice, WITHOUT
interrupting the conversation he is having with the main bot. So:

  * it is NOT a turn in the main bot's session (no job_ctl follow-up): it sends its own Telegram
    message through the main bot's token, so the live session and its context stay untouched;
  * the check is model-free and cheap (Ubuntu's CVE JSON + apt + uname + a few files);
  * a model is called ONLY when something changed, in a fresh one-shot `claude -p` with no tools,
    to turn the facts into short advice; if that fails, a fixed template is sent instead;
  * the first run records a baseline and sends one "watch started" line; after that, silence until
    a change. Three failed checks in a row send one "cannot check" message.
  * when a watch reaches its end (fixed kernel running, stopgap removed) it says so once and retires.

Nothing here changes the system. It reads and reports; every action stays the owner's decision.

    python3 tools/secwatch/secwatch.py            # the cron run
    python3 tools/secwatch/secwatch.py --dry      # print what it sees and would send, send nothing
    python3 tools/secwatch/secwatch.py --test     # send the current status as a test message
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STATE = HERE / "state.json"
LOG = HERE / "secwatch.log"
WATCHES = HERE / "watches.json"
ENV = ROOT / "bot" / ".env"
HEAD = "🛡 Сторож безопасности"


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def sh(*cmd: str, timeout: int = 60) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


# ── observations ─────────────────────────────────────────────────────────────────────────────────
def observe_kernel_cve(w: dict) -> dict:
    """Everything that decides where we are on the road from 'vulnerable' to 'done'."""
    o: dict = {}
    req = urllib.request.Request(f"https://ubuntu.com/security/cves/{w['cve']}.json",
                                 headers={"User-Agent": "krevetka-secwatch"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    o["ubuntu_priority"] = d.get("priority")
    for p in d.get("packages", []):
        if p.get("name") == w["package"]:
            for s in p.get("statuses", []):
                if s.get("release_codename") == w["release"]:
                    o["ubuntu_status"] = s.get("status")
                    o["ubuntu_version"] = s.get("description")
    pol = sh("apt-cache", "policy", w["meta"])
    m_inst = re.search(r"Installed:\s*(\S+)", pol)
    m_cand = re.search(r"Candidate:\s*(\S+)", pol)
    o["meta_installed"] = m_inst.group(1) if m_inst else None
    o["meta_candidate"] = m_cand.group(1) if m_cand else None
    imgs = re.findall(r"^ii\s+linux-image-(\d[\w.\-]*-generic)\s", sh("dpkg", "-l", "linux-image-*"), re.M)
    o["newest_installed_image"] = max(imgs, key=ver_tuple) if imgs else None
    o["running"] = os.uname().release
    o["reboot_required"] = Path("/var/run/reboot-required").exists()
    # WHY a reboot is wanted: any security update (libc, systemd...) raises the same flag, and
    # without the package list the advice would blame the kernel for it.
    pkgs = Path("/var/run/reboot-required.pkgs")
    o["reboot_pkgs"] = sorted(set(pkgs.read_text().split()))[:10] if o["reboot_required"] and pkgs.exists() else []
    o["stopgap_active"] = sh("systemctl", "is-active", w["stopgap_unit"]).strip() == "active"
    return o


def fixed_version(w: dict, o: dict) -> tuple:
    """Ubuntu's own version wins when it is higher: if the fix slips from 149 to 151, a plain 149
    must not be taken for fixed. watches.json's value is only the floor."""
    return max(ver_tuple(w["fixed"]), ver_tuple(o.get("ubuntu_version") or ""))


def stage_kernel_cve(w: dict, o: dict) -> str:
    fixed = fixed_version(w, o)
    if ver_tuple(o["running"]) >= fixed:
        return "done" if not o["stopgap_active"] else "running_fixed_stopgap_on"
    if o.get("newest_installed_image") and ver_tuple(o["newest_installed_image"]) >= fixed:
        return "installed_reboot_needed"
    if o.get("meta_candidate") and ver_tuple(o["meta_candidate"]) >= fixed:
        return "available_not_installed"
    if o.get("ubuntu_status") == "released":
        return "released_not_in_apt_yet"
    return "waiting"


STAGE_TEXT = {
    "waiting": "Исправления пока нет, ждём. Заглушка в фаерволе работает.",
    "released_not_in_apt_yet": "Ubuntu отметила исправление выпущенным, но на сервер оно ещё не пришло. "
                               "Автообновление подтянет его само в ближайшие сутки.",
    "available_not_installed": "Исправленное ядро доступно, но ещё не установлено. Автообновление "
                               "обычно ставит его само ночью; можно и вручную.",
    "installed_reboot_needed": "Исправленное ядро установлено, но работает старое: нужна перезагрузка "
                               "сервера. Сама она не случится.",
    "running_fixed_stopgap_on": "Сервер работает на исправленном ядре. Заглушку в фаерволе можно снять.",
    "done": "Исправленное ядро работает, заглушка снята. Наблюдение завершено.",
}


# ── message ──────────────────────────────────────────────────────────────────────────────────────
def facts_text(w: dict, o: dict, stage: str) -> str:
    return (f"{w['title']}\n"
            f"Этап: {STAGE_TEXT[stage]}\n"
            f"Ubuntu: {o.get('ubuntu_status')} {o.get('ubuntu_version') or ''} (приоритет {o.get('ubuntu_priority')})\n"
            f"Ядро работает: {o['running']}; новейшее установленное: {o.get('newest_installed_image')}; "
            f"в репозитории: {o.get('meta_candidate')}\n"
            f"Нужна перезагрузка: {'да (' + ', '.join(o.get('reboot_pkgs') or ['?']) + ')' if o['reboot_required'] else 'нет'}; "
            f"заглушка в фаерволе: {'включена' if o['stopgap_active'] else 'выключена'}")


def advice(w: dict, o: dict, stage: str, changed: list[str]) -> str:
    prompt = (
        "Ты помощник администратора сервера. Ниже факты о ходе закрытия уязвимости ядра на сервере "
        "Ubuntu 24.04, где работают сайт, база, Matrix и Telegram-боты владельца. Напиши владельцу "
        "3-5 коротких строк по-русски, простыми словами: что изменилось, есть ли смысл что-то делать "
        "сейчас, и что именно (или что можно спокойно ждать). Не выдумывай фактов, которых нет ниже. "
        "Перезагрузка - решение владельца; если она нужна, предложи сделать её через основного бота "
        "в удобное время. Только обычные дефисы, без длинных тире. Без заголовков и markdown.\n\n"
        f"Уязвимость: {w['cve']} ({w['summary']}). Исправленная версия ядра: "
        f"{'.'.join(map(str, fixed_version(w, o)))}. "
        f"Заглушка: {w['stopgap_note']}\n"
        f"Что изменилось с прошлой проверки: {', '.join(changed) or 'первая проверка'}\n"
        f"Факты:\n{facts_text(w, o, stage)}\n"
    )
    try:
        p = subprocess.run(["claude", "-p", "--model", "claude-opus-5-5", "--effort", "medium",
                            "--disallowedTools", "Bash", "Edit", "Write", "Read", "Glob", "Grep",
                            "WebFetch", "WebSearch", "Task"],
                           # prompt on stdin: --disallowedTools is variadic and would eat it as a tool name
                           input=prompt, capture_output=True, text=True, timeout=240, cwd="/tmp")
        text = (p.stdout or "").strip().replace("—", "-").replace("–", "-")
        if p.returncode == 0 and 20 < len(text) < 1500:
            return text
        log(f"advice: model rc={p.returncode} len={len(text)}")
    except Exception as e:
        log(f"advice: model failed {e}")
    return STAGE_TEXT[stage]


def send(text: str) -> bool:
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z_]+)=(.*)", line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"')
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), (env.get("ALLOWED_USERS") or "").split(",")[0].strip()
    if not token or not chat:
        log("send: no token/chat")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        log(f"send failed: {type(e).__name__}")   # never log the URL: it carries the token
        return False


# ── main ─────────────────────────────────────────────────────────────────────────────────────────
KEYS = ("ubuntu_status", "ubuntu_version", "meta_candidate", "newest_installed_image", "running",
        "reboot_required", "stopgap_active")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()

    watches = json.loads(WATCHES.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}

    for w in watches:
        wid = w["id"]
        st = state.setdefault(wid, {})
        if st.get("retired") and not a.test:
            continue
        try:
            o = observe_kernel_cve(w)
            st["fails"] = 0
        except Exception as e:
            st["fails"] = st.get("fails", 0) + 1
            log(f"{wid}: check failed ({st['fails']}): {type(e).__name__}: {e}")
            if st["fails"] == 3 and not a.dry:
                send(f"{HEAD}\n{w['title']}\nНе могу проверить уже 3 раза подряд ({type(e).__name__}). "
                     f"Подробности в tools/secwatch/secwatch.log - спросите основного бота.")
            continue
        stage = stage_kernel_cve(w, o)
        prev = st.get("obs")
        changed = [k for k in KEYS if prev and prev.get(k) != o.get(k)]
        if prev and st.get("stage") != stage:
            changed.insert(0, f"этап: {st.get('stage')} -> {stage}")
        log(f"{wid}: stage={stage} changed={changed or '-'}")

        msg = None
        if a.test:
            msg = f"{HEAD} (проверка)\n{facts_text(w, o, stage)}"
        elif prev is None:
            msg = (f"{HEAD}\nНаблюдение запущено: {w['title']}\n{STAGE_TEXT[stage]}\n"
                   f"Проверяю каждые 3 часа и напишу сюда только если что-то изменится.")
        elif changed:
            msg = f"{HEAD}\n{w['title']}\n\n{advice(w, o, stage, changed)}\n\n{facts_text(w, o, stage)}"

        if msg:
            if a.dry:
                print(msg + "\n---")
            elif send(msg):
                log(f"{wid}: sent")
            else:
                continue  # not delivered -> keep the old state so the change is reported next time
        if not a.dry and not a.test:
            st["obs"], st["stage"] = o, stage
            if stage == "done":
                st["retired"] = True
        if a.dry:
            print(json.dumps({"stage": stage, **o}, ensure_ascii=False, indent=1))

    if not a.dry and not a.test:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
