# markdown-new — Docs

## TL;DR

Команда: `python3 ~/.claude/skills/markdown-new/scripts/markdown_new_fetch.py '<URL>'`
Методы: `--method auto` (по умолчанию), `ai`, `browser` (для SPA/JS-страниц)
Только публичные HTTPS без логина и без токенов/секретов в URL.
Сохранить в файл: добавить `--output /tmp/result.md`

## Установка

Установлен в: `~/.claude/skills/markdown-new/`

## Команда запуска

```
python3 ~/.claude/skills/markdown-new/scripts/markdown_new_fetch.py '<URL>'
```

## Параметры

- `--method auto|ai|browser` — метод конвертации (auto по умолчанию)
- `--output <file>` — сохранить результат в файл
- `--deliver-md` — обернуть в теги <url>...</url>, удобно для длинного контента
- `--retain-images true|false` — сохранять картинки (false для конспектов, true если важны)

## Примеры

Быстро прочитать статью:
```
python3 ~/.claude/skills/markdown-new/scripts/markdown_new_fetch.py 'https://example.com/article'
```

Страница с JS (SPA):
```
python3 ~/.claude/skills/markdown-new/scripts/markdown_new_fetch.py 'https://example.com' --method browser
```

Длинный документ, сохранить в файл:
```
python3 ~/.claude/skills/markdown-new/scripts/markdown_new_fetch.py 'https://docs.example.com' --output /tmp/docs.md --deliver-md
```

## Типовые кейсы

- "Прочитай доку по X и объясни" → да
- "Статья / README на GitHub / публичная документация" → да
- "Личный кабинет / внутренний URL / 192.168.x.x" → нет (см. policy)
- "Ссылка с ?token= / ?signature=" → нет (см. policy)
- "Юридический текст, нужна точная формулировка" → нет (см. policy)
