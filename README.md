# lil_worker — Telegram → Claude bridge

Telegram-бот, який пропускає повідомлення через Claude Code CLI і відповідає назад у чат.
Підтримує текст, голосові повідомлення, фото/альбоми, довгі відповіді, інструменти
(Read, Write, Bash, WebFetch і т.д.) та скіли.

---

## Що вміє

- Прийом текстових повідомлень → Claude відповідає
- Голосові повідомлення → транскрипція (OpenAI) → Claude
- Фото та альбоми → передача в Claude як base64
- Документи → зберігаються в `.inbox/`, шлях передається агенту разом з підписом
- Довгі відповіді автоматично розбиваються на частини по 4000 символів
- Стрімінг: нотифікації про кожен інструмент надходять у реальному часі
- Markdown → Telegram HTML конвертація
- Сесії між повідомленнями (`/new` — нова сесія)
- Whitelist користувачів (тільки дозволені Telegram ID)
- Перемикання моделі через `model_config.json` без рестарту
- Скіли (`skills/`) — підключені через симлінк `.claude/skills`, працюють одразу після клону

---

## Команди в чаті

- `/new` — скинути сесію (почати розмову з чистого листа)
- `/status` — модель, стан сесії, аптайм
- `/provider claude|codex` — перемкнути CLI-провайдера (Codex опційно)

Будь-який інший текст (у тому числі з `/`) йде агенту як звичайне повідомлення.

---

## Структура

```
bot/
├── krevetka.py             # основний код бота (НЕ bot.py — див. нижче)
├── run.sh                  # менеджер процесу (start/stop/restart/status)
├── watchdog.sh             # перезапуск при падінні
├── validate.sh             # перевірка перед рестартом (syntax/imports/dry-run)
├── selfmod_guard.py        # захист коду бота від правок вторинними інстансами
├── instance.sh             # додаткові інстанси бота (опційно)
├── requirements.txt        # Python залежності
├── .env                    # конфіг (токени, дозволені юзери) — створюється setup.sh
├── .env.example            # приклад конфігу
├── model_config.json       # поточна модель Claude
├── transcribe_config.json  # мова транскрипції
├── .sessions.json          # сесії розмов (auto)
├── lil_worker.log          # лог (auto)
└── .venv/                  # Python venv (auto після setup)
CLAUDE.md                   # інструкції агента (identity + rules)
skills/                     # скіли агента
tools/                      # утиліти (пам'ять, durable-джоби, створення скілів)
docs/ policies/             # документація та політики інструментів
```

Файл входу навмисно називається `krevetka.py`, а не `bot.py`: на сервері часто крутяться чужі
проєкти зі своїм `bot.py`, і нечіткий `pkill -f bot.py` вбивав не того. Ця назва не має спільного
підрядка з `bot.py`, тому промахнутись неможливо.

---

## Вимоги

- Ubuntu 20.04+ / Debian
- Python 3.10+
- [Claude Code CLI](https://claude.ai/code) встановлений і авторизований (`claude` доступний глобально)
- Акаунт Anthropic з підпискою
- Telegram Bot Token (від @BotFather)
- OpenAI API Key (для транскрипції голосу, опційно)

---

## Швидке розгортання (одна команда)

```bash
ssh root@your-server-ip
curl -fsSL https://raw.githubusercontent.com/naantragar/lil-worker-public/main/install.sh | bash
```

Скрипт автоматично встановить все потрібне (git, Node.js, Claude CLI, Python venv, залежності).
Після цього залишиться три кроки:

```bash
claude login                           # авторизація (відкриє посилання)
cd ~/lil_worker && bash setup.sh       # введення токенів
bot/run.sh start                       # запуск
```

---

## Розгортання покроково (ручний варіант)

Що потрібно заздалегідь:
- VPS з Ubuntu 20.04+ (або Debian)
- SSH-доступ до сервера (root або sudo-користувач)
- Акаунт Anthropic з підпискою (для Claude Code CLI)
- Telegram Bot Token (від @BotFather)
- (опційно) OpenAI API Key (для транскрипції голосових повідомлень)

---

### Крок 1: Підключитись до VPS

З локального комп'ютера:

```bash
ssh root@your-server-ip
```

Або якщо є окремий користувач:

```bash
ssh username@your-server-ip
```

---

### Крок 2: Встановити базові пакети

Оновити систему та встановити git, curl, Node.js:

```bash
sudo apt update && sudo apt install -y git curl
```

Встановити Node.js 22 (потрібен для Claude Code CLI):

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -
sudo apt install -y nodejs
```

Перевірити:

```bash
node --version
# очікуємо v22.x.x
```

---

### Крок 3: Встановити Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
```

Авторизуватись (відкриє посилання в браузері — скопіювати його та відкрити на будь-якому пристрої):

```bash
claude login
```

Слідувати інструкціям в терміналі. Після успішної авторизації перевірити:

```bash
claude --version
```

---

### Крок 4: Завантажити код бота

```bash
cd ~
git clone https://github.com/naantragar/lil-worker-public.git lil_worker
cd lil_worker
```

Перевірити що файли на місці:

```bash
ls bot/
# має показати: krevetka.py  run.sh  requirements.txt  та інші
```

---

### Крок 5: Запустити setup

```bash
bash setup.sh
```

Скрипт автоматично:
- Перевірить наявність Python (встановить якщо нема)
- Створить Python virtual environment
- Встановить залежності (aiogram, mistune, openai, lingua)

Потім запитає три речі:
- `TELEGRAM_BOT_TOKEN:` — токен від @BotFather (довгий рядок типу `123456:ABC-DEF...`)
- `ALLOWED_USERS:` — Telegram ID користувачів через кому (наприклад `123456789,987654321`)
- `OPENAI_API_KEY:` — ключ OpenAI для голосових (або просто Enter щоб пропустити)

Як дізнатись свій Telegram ID: написати боту @userinfobot в Telegram.

---

### Крок 6: Запустити бота

```bash
bot/run.sh start
```

Перевірити що працює:

```bash
bot/run.sh status
# має показати: Running (PID xxxxx)
```

Якщо щось не так — подивитись логи:

```bash
tail -n 50 bot/lil_worker.log
```

**Ніколи не запускати** `bot/run.sh logs`, `tail -f`, `top`, `less` та інші інтерактивні команди
з-під агента — вони не завершуються і вішають хід.

---

### Крок 7: Перевірити в Telegram

Відкрити бота в Telegram і написати будь-яке повідомлення.
Бот має відповісти протягом кількох секунд.

---

### Управління після встановлення

```bash
cd ~/lil_worker

bot/run.sh start     # запустити
bot/run.sh stop      # зупинити
bot/run.sh restart   # перезапустити
bot/run.sh status    # перевірити статус

tail -n 50 bot/lil_worker.log   # подивитись логи
```

---

## Конфіг (.env)

```env
TELEGRAM_BOT_TOKEN=your_token_here
ALLOWED_USERS=123456789,987654321
OPENAI_API_KEY=sk-...
OPENAI_VOICE_MODEL=gpt-4o-mini-transcribe
```

`ALLOWED_USERS` — Telegram user ID через кому. Всі інші ігноруються.
Повний перелік ключів — у `bot/.env.example`.

---

## Перемикання моделі

Claude: редагувати `bot/model_config.json` — набуває чинності з наступного повідомлення,
рестарт не потрібен. Краще вказувати явний id, а не аліас, щоб модель не «попливла»:

```json
{ "model": "claude-opus-5" }      // флагман, дефолт
{ "model": "claude-sonnet-5" }    // швидше й дешевше для рутини
{ "model": "claude-haiku-4-5" }   // найшвидша й найдешевша
```

Аліаси `opus` / `sonnet` / `haiku` теж працюють — CLI сам резолвить їх у свою поточну модель.
Перед тим як прописати новий id, перевірити що він живий:

```bash
claude -p --model <id> "Reply OK"
```

Codex (опційно): `bot/codex_model_config.json`, теж без рестарту:

```json
{ "model": "default" }
{ "model": "gpt-5.4" }
{ "model": "gpt-5.4-mini" }
```

---

## Транскрипція голосу

`bot/transcribe_config.json`:

```json
{ "language": null, "temperature": 0.2 }    // авто-детект
{ "language": "uk", "temperature": 0.1 }    // фіксована мова
{ "language": "ru", "temperature": 0.1 }
{ "language": "en", "temperature": 0.1 }
```

---

## Скіли

`skills/<name>/SKILL.md` — набір готових умінь (фронтенд-дизайн, конвертація сторінок у Markdown,
brainstorm, індексація репозиторію тощо). Виявляються через симлінк `.claude/skills → ../skills`,
який уже є в репозиторії, тому працюють одразу після клону.

Агент може створювати нові скіли сам (`tools/new_skill.py scaffold|validate|list`) — правила
описані в `CLAUDE.md`.
