# lil_worker — Telegram → Claude bridge

Telegram-бот, який пропускає повідомлення через Claude Code CLI і відповідає назад у чат.
Підтримує текст, голосові повідомлення, фото/альбоми, довгі відповіді, інструменти (Read, Write, Bash, WebFetch і т.д.).

---

## Що вміє

- Прийом текстових повідомлень → Claude відповідає
- Голосові повідомлення → транскрипція (OpenAI Whisper) → Claude
- Фото та альбоми → передача в Claude як base64
- Довгі відповіді автоматично розбиваються на частини по 4000 символів
- Стрімінг: нотифікації про кожен інструмент надходять у реальному часі
- Markdown → Telegram HTML конвертація
- Автодетект мови (Ukrainian / Russian / English)
- Сесії між повідомленнями (`/new` — нова сесія, `/status` — стан)
- Whitelist користувачів (тільки дозволені Telegram ID)
- Перемикання моделі через `model_config.json` без рестарту

---

## Структура

```
bot/
├── bot.py                  # основний код бота
├── run.sh                  # менеджер процесу (start/stop/restart/status)
├── requirements.txt        # Python залежності
├── .env                    # конфіг (токени, дозволені юзери)
├── model_config.json       # поточна модель Claude
├── transcribe_config.json  # мова транскрипції
├── .sessions.json          # сесії розмов (auto)
├── lil_worker.log            # лог (auto)
└── .venv/                  # Python venv (auto після setup)
CLAUDE.md                   # база знань агента (identity + rules)
```

---

## Вимоги

- Ubuntu 20.04+ / Debian
- Python 3.10+
- [Claude Code CLI](https://claude.ai/code) встановлений і авторизований (`claude` доступний глобально)
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

Авторизуватись (відкриє посилання в браузері - скопіювати його та відкрити на будь-якому пристрої):

```bash
claude login
```

Слідувати інструкціям в терміналі. Після успішної авторизації перевірити:

```bash
claude --version
# має показати версію, наприклад: claude 1.x.x
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
# має показати: bot.py  requirements.txt  run.sh  та інші
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
- `TELEGRAM_BOT_TOKEN:` - токен від @BotFather (довгий рядок типу `123456:ABC-DEF...`)
- `ALLOWED_USERS:` - Telegram ID користувачів через кому (наприклад `123456789,987654321`)
- `OPENAI_API_KEY:` - ключ OpenAI для голосових (або просто Enter щоб пропустити)

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

Якщо щось не так - подивитись логи:

```bash
tail -n 50 bot/lil_worker.log
```

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
CLAUDE_MODEL=sonnet
OPENAI_API_KEY=sk-...
OPENAI_VOICE_MODEL=gpt-4o-mini-transcribe
```

`ALLOWED_USERS` — Telegram user ID через кому. Всі інші ігноруються.

---

## Управління

```bash
cd bot/
./run.sh start    # запуск
./run.sh stop     # зупинка
./run.sh restart  # рестарт
./run.sh status   # статус

tail -n 50 lil_worker.log   # перегляд логів
```

---

## Перемикання моделі

Claude: редагувати `bot/model_config.json` — набуває чинності з наступного повідомлення, рестарт не потрібен:

```json
{ "model": "sonnet" }   // claude-sonnet-4-6
{ "model": "opus" }     // claude-opus-4-6
{ "model": "haiku" }    // claude-haiku-4-5
```

Codex: редагувати `bot/codex_model_config.json` — теж набуває чинності з наступного повідомлення, без рестарту:

```json
{ "model": "default" }      // використовувати модель Codex CLI за замовчуванням
{ "model": "gpt-5.4" }      // явне задання моделі
{ "model": "gpt-5.4-mini" }
{ "model": "gpt-5.3-codex" }
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
