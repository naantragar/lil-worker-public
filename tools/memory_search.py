#!/usr/bin/env python3
"""
memory_search — полнотекстовый поиск по моей памяти (SQLite FTS5).

Зачем: вместо слепого перечитывания всех файлов — быстрый поиск по
sessions/, knowledge/ и долговременной памяти. Это «дешёвый» порт фичи
из an upstream agent (FTS5 session search).

Индексируемые корни (по умолчанию):
  - ~/lil_worker/sessions
  - ~/lil_worker/knowledge
  - /root/.claude/projects/-home-takopi-b-lil-worker/memory   (MEMORY.md + факты)

Использование:
  python3 memory_search.py search "<запрос>" [--limit N]
  python3 memory_search.py index            # пересобрать индекс, показать счётчик
  python3 memory_search.py stats            # что проиндексировано

Индекс пересобирается при каждом запуске (корпус маленький), поэтому
всегда свежий — отдельно вызывать index не обязательно.
"""
import os
import re
import sqlite3
import sys

# Базу путей выводим из расположения самого скрипта, а не хардкодим:
#   BASE  = корень репо (на шаг выше tools/)  → деплоится в любой папке/у любого юзера
#   slug  = тот же ключ, что генерит Claude Code из cwd (каждый не-буквенно-цифровой
#           символ → '-'); напр. ~/lil_worker → -home-takopi-b-lil-worker
#   MEMORY = ~/.claude/projects/<slug>/memory  (служебное хранилище харнесса)
# Переопределить можно через env: KREVETKA_BASE / KREVETKA_MEMORY_DIR.
BASE = os.environ.get(
    "KREVETKA_BASE",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
_slug = re.sub(r"[^A-Za-z0-9]", "-", BASE)
MEMORY_DIR = os.environ.get(
    "KREVETKA_MEMORY_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "projects", _slug, "memory"),
)
ROOTS = [
    os.path.join(BASE, "sessions"),
    os.path.join(BASE, "knowledge"),
    MEMORY_DIR,
]
DB_PATH = os.path.join(BASE, ".memory_index.db")
EXTS = (".md", ".txt")
MAX_BYTES = 400_000  # пропускать аномально большие файлы


def iter_files(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith("."):
                    continue
                if not name.lower().endswith(EXTS):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) > MAX_BYTES:
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                title = first_heading(text) or name
                yield path, title, text


def first_heading(text):
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
        if s:
            return s[:80]
    return ""


def build_index(conn):
    conn.executescript(
        """
        DROP TABLE IF EXISTS docs;
        CREATE VIRTUAL TABLE docs USING fts5(
            path, title, body,
            tokenize = "unicode61 remove_diacritics 2"
        );
        """
    )
    n = 0
    for path, title, body in iter_files(ROOTS):
        conn.execute(
            "INSERT INTO docs (path, title, body) VALUES (?, ?, ?)",
            (path, title, body),
        )
        n += 1
    conn.commit()
    return n


def make_match(query):
    """Безопасно превратить произвольный запрос в FTS5 MATCH:
    берём слова (буквы/цифры), каждое как префиксный терм, объединяем через OR."""
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return None
    return " OR ".join(f'"{t}"*' for t in terms)


def cmd_search(conn, query, limit):
    match = make_match(query)
    if not match:
        print("Пустой запрос.")
        return
    rows = conn.execute(
        """
        SELECT path, title,
               snippet(docs, 2, '«', '»', ' … ', 12) AS snip,
               bm25(docs) AS rank
        FROM docs
        WHERE docs MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    if not rows:
        print(f"Ничего не найдено по: {query}")
        return
    print(f"Найдено {len(rows)} (по запросу: {query})\n")
    for i, (path, title, snip, rank) in enumerate(rows, 1):
        snip = " ".join(snip.split())
        print(f"{i}. {title}")
        print(f"   {path}")
        print(f"   {snip}\n")


def cmd_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    print(f"Проиндексировано документов: {total}")
    for root in ROOTS:
        c = conn.execute(
            "SELECT COUNT(*) FROM docs WHERE path LIKE ?", (root + "%",)
        ).fetchone()[0]
        print(f"  {root}: {c}")


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    conn = sqlite3.connect(DB_PATH)
    count = build_index(conn)

    if cmd == "index":
        print(f"Индекс пересобран: {count} документов -> {DB_PATH}")
        return 0
    if cmd == "stats":
        cmd_stats(conn)
        return 0
    if cmd == "search":
        rest = argv[1:]
        limit = 8
        if "--limit" in rest:
            i = rest.index("--limit")
            try:
                limit = int(rest[i + 1])
            except (IndexError, ValueError):
                limit = 8
            rest = rest[:i] + rest[i + 2:]
        query = " ".join(rest).strip()
        if not query:
            print("Использование: memory_search.py search \"<запрос>\"")
            return 1
        cmd_search(conn, query, limit)
        return 0

    print(f"Неизвестная команда: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
