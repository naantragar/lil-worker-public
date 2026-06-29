# Pinchtab — Docs

## TL;DR

Лестница чтения: `navigate` → `text` → `snapshot_interactive` → `snapshot_full`
Управление: `~/.pinchtab/run.sh start|stop|restart|status|foreground`
Escape hatch: `POST /evaluate` — произвольный JS в контексте страницы (перехват загрузок, DOM-скрапинг)
Стоимость: text ~800 токенов, snapshot_interactive ~3600, snapshot_full ~10500
Только публичные HTTPS URL. Блокируются: localhost, IP, токены в параметрах.
⚠️ Перед остановкой: `browser.sh navigate "about:blank"` на каждой тяжёлой вкладке. Chrome восстанавливает сессию при следующем старте — SEMrush и другие SPA перегрузятся и убьют CPU. Подробности: `policies/pinchtab.md`

## Архитектура

Pinchtab (Go-бинарник) → CDP → Chrome (в Docker-контейнере headless)
Оба работают на localhost, доступ только через Bearer-токен.

## Файлы

- Бинарник: `~/.local/bin/pinchtab`
- Конфиг/профиль/логи: `~/.pinchtab/`
- Wrapper-скрипт: `~/.pinchtab/browser.sh`
- Управление: `~/.pinchtab/run.sh start|stop|restart|status|foreground`

## Wrapper-команды (browser.sh)

Все команды автоматически добавляют Bearer-токен. URL-валидация встроена.

### Чтение

```
browser.sh navigate <url>         — перейти на страницу
browser.sh text                   — получить текст (дешёвый, readable)
browser.sh snapshot_interactive   — интерактивные элементы (compact, ~75% экономии)
browser.sh snapshot_full          — полное дерево доступности (дорого)
```

### Действия

```
browser.sh click <ref>            — клик по элементу (ref из snapshot)
browser.sh type <ref> <text>      — ввод текста в поле
browser.sh scroll <up|down>       — прокрутка
```

### Сервис

```
browser.sh tabs                   — список открытых вкладок
browser.sh health                 — статус Pinchtab
```

## Управление сервисом (run.sh)

```
~/.pinchtab/run.sh start          — запустить Chrome (Docker) + Pinchtab
~/.pinchtab/run.sh stop           — остановить оба
~/.pinchtab/run.sh restart        — перезапустить
~/.pinchtab/run.sh status         — статус + health-check
~/.pinchtab/run.sh foreground     — запустить Pinchtab в foreground с уже экспортированным env
```

## Важная ловушка env

Нельзя запускать Pinchtab так:

```bash
source ~/.pinchtab/.env && ~/.local/bin/pinchtab
```

Почему: переменные из `source` становятся shell-variable, но не попадают в environment child process без `export`.
Из-за этого `CDP_URL` не доходит до бинаря, и он может ошибочно пытаться стартовать локальный `google-chrome`.

Безопасные варианты:

```bash
~/.pinchtab/run.sh foreground
```

или

```bash
export $(grep -v '^#' ~/.pinchtab/.env | xargs)
~/.local/bin/pinchtab
```

## URL-валидация (встроена в browser.sh)

Блокируются автоматически:
- Не-HTTP(S) URL
- localhost, 127.x, 10.x, 192.168.x, 172.16-31.x, .local
- Сырые IP-адреса
- URL с параметрами: token=, key=, signature=, auth=, session=, secret=, password=

## Типовой сценарий использования

```
# 90% случаев: navigate → text → ответ
browser.sh navigate "https://docs.example.com/api"
browser.sh text

# Если текст неполный: добавить snapshot
browser.sh snapshot_interactive

# Если нужен клик (cookie-баннер, expand):
browser.sh click e3
browser.sh text
```

## Endpoint /evaluate — выполнение JavaScript

Позволяет выполнить произвольный JS в контексте текущей страницы. Это escape hatch для задач, которые стандартные команды не покрывают (перехват загрузок, DOM-скрапинг таблиц и т.д.).

**API**: `POST /evaluate` с JSON `{"expression": "...JS code..."}`
**Через curl** (browser.sh пока не имеет обёртки):
```bash
source ~/.pinchtab/.env
BASE="http://127.0.0.1:${BRIDGE_PORT:-9867}"
AUTH="Authorization: Bearer $BRIDGE_TOKEN"
curl -sf -H "$AUTH" -X POST "$BASE/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"expression": "document.title"}'
# → {"result":"Page Title"}
```

**Особенности:**
- Возвращает `{"result": ...}` — строку или объект
- Async-функции возвращают `{"result":{}}` (Promise) — нужно использовать `.then()` + `window.__variable`
- Для больших данных (>15000 символов) — сохранять в `window.__var`, забирать порциями через `.substring()`
- JS выполняется в контексте страницы со всеми cookies/auth текущей сессии

### Типичные сценарии

**1. Перехват файловых загрузок (blob/link intercept):**
```js
// Инжектируем ПЕРЕД кликом на Export
(function(){
  window.__dlLog=[];
  var origClick=HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click=function(){
    if(this.href||this.download){
      window.__dlLog.push({href:this.href,download:this.download,time:Date.now()});
    }
    return origClick.apply(this,arguments);
  };
  // Перехват createElement для <a> с download
  var origCreateEl=document.createElement;
  document.createElement=function(tag){
    var el=origCreateEl.call(document,tag);
    if(tag.toLowerCase()==="a"){
      setTimeout(function(){
        if(el.href&&(el.href.includes("export")||el.href.includes("download"))){
          window.__dlLog.push({href:el.href,download:el.download,created:true});
        }
      },100);
    }
    return el;
  };
  return "ok";
})()
```
Затем: кликаем Export → CSV → читаем `window.__dlLog` → делаем `fetch(url)` внутри браузера.

**2. DOM-скрапинг таблиц (role=row):**
```js
(function(){
  var rows=document.querySelectorAll("[role=row]");
  var csv=[];
  for(var i=0;i<rows.length;i++){
    var cells=rows[i].querySelectorAll("[role=cell],[role=gridcell],[role=columnheader]");
    if(cells.length<3) continue;
    var r=[];
    for(var j=1;j<cells.length;j++){
      r.push('"'+cells[j].innerText.trim().replace(/\n/g," ").replace(/,/g,";")+'"');
    }
    csv.push(r.join(","));
  }
  window.__tableCSV=csv.join("\n");
  return "rows: "+csv.length;
})()
```

**3. Fetch скачанного URL внутри браузера (с cookies):**
```js
fetch("https://...export-url...")
  .then(function(r){return r.text()})
  .then(function(t){window.__csvContent=t; window.__csvStatus="done "+t.length})
  .catch(function(e){window.__csvStatus="error: "+e.message});
"fetching..."
```
Затем: проверяем `window.__csvStatus`, забираем `window.__csvContent.substring(0,15000)` порциями.

## Стоимость токенов (ориентировочно)

- text: ~800 токенов (самый дешёвый)
- snapshot interactive: ~3600 токенов
- snapshot full: ~10500 токенов

## Troubleshooting

Pinchtab не отвечает:
```
~/.pinchtab/run.sh status         — проверить статус
~/.pinchtab/run.sh restart        — перезапустить оба
tail -n 20 ~/.pinchtab/pinchtab.log  — посмотреть логи
```

Chrome упал:
```
docker ps -a | grep pinchtab-chrome
~/.pinchtab/run.sh restart
```
