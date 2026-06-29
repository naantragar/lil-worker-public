# Docs: SEMrush — навигация, инструменты, интерфейс

## TL;DR

Доступ: semrush.com через Pinchtab (headless Chrome), логин email+pass
Инструменты: Keyword Magic Tool, Keyword Overview, Organic Research
Экспорт CSV: JS-перехват через `/evaluate` (рекомендуется). Скрипты: `seo/sourcechain/export_csv.sh`
Только read-only. Ничего не создавать/удалять/менять (projects, settings, billing).
Страну выбирать ДО поиска. Фильтры — бесплатны, не расходуют лимит.

## Доступ

- URL: https://www.semrush.com
- Логин: через Pinchtab — navigate на /login/, ввести email+password через type
- После логина: автоматический редирект на dashboard

## Страница логина

Элементы:
- e10: textbox "Email"
- e11: textbox "Password"
- e8: button "Log in"
- e12: button "View password" (показать пароль)

Процедура входа:
1. `browser.sh navigate "https://www.semrush.com/login/"`
2. `browser.sh snapshot_interactive` — найти элементы (номера могут меняться!)
3. Cookie-баннер: если виден — `browser.sh click eN` (Allow all cookies)
4. `browser.sh type e10 "email@example.com"` — type сразу целит в элемент, click не нужен
5. `browser.sh type e11 "password"`
6. `browser.sh click e8` (Log in)
7. Подождать 3-5 сек (sleep), затем `browser.sh text` — проверить что залогинились
8. Возможно: 2FA / CAPTCHA — если появится, сообщить пользователю

ВАЖНО: номера элементов (e8, e10, e11) могут отличаться от сессии к сессии!
ВСЕГДА делать snapshot_interactive перед логином и ориентироваться по labels (Email, Password, Log in).

## Навигация (левый сайдбар, после логина)

Основные тулкиты:
- **SEO Toolkit** — главный для нас
  - Keyword Research: Keyword Overview, Keyword Magic Tool, Keyword Strategy Builder, Position Tracking
  - Competitive Research: Domain Overview, Organic Rankings, Top Pages, Keyword Gap
- Traffic & Market Toolkit
- Advertising Toolkit
- Content Toolkit
- Social Toolkit
- (и другие — нам НЕ нужны)

## Keyword Magic Tool — основной инструмент

### URL
`https://www.semrush.com/analytics/keywordmagic/`

### Интерфейс (сверху вниз)

**Верхняя панель:**
- Поле ввода seed-ключа (textbox)
- Dropdown выбора страны/базы данных (ВЫБРАТЬ ДО поиска!)
  - Brazil — для pt-BR ключей
  - Mexico — для es-MX ключей
- Кнопка Search/Find Keywords

**Строка match types (под поисковой строкой):**
- All Keywords — все типы совпадений
- Broad Match — seed в любых вариациях (больше всего результатов)
- Phrase Match — seed в точной форме, порядок слов может меняться
- Exact Match — seed в точной форме И порядке
- Related — семантически связанные (могут НЕ содержать seed)

**Кнопка Questions** — отфильтровать только вопросы (who, what, how...)

**Левый сайдбар — группы ключей:**
- Автоматическая группировка по темам
- До 3 уровней вложенности
- Сортировка: по количеству ключей ИЛИ по суммарному volume
- Клик по группе = фильтрация таблицы

**Основная таблица — колонки:**
- Keyword
- Intent (I = Informational, N = Navigational, C = Commercial, T = Transactional)
- Volume (среднемесячный объём поиска)
- KD% (Keyword Difficulty)
- CPC (стоимость клика в рекламе, USD)
- Competitive Density
- SERP Features (иконки: featured snippets, PAA, video и т.д.)

**Фильтры (панель фильтров над таблицей):**
- Volume — min/max
- KD% — min/max
- Intent — чекбоксы I/N/C/T
- CPC — min/max
- Include Keywords — слова, которые ДОЛЖНЫ быть
- Exclude Keywords — слова, которых НЕ должно быть
- Word Count — количество слов в ключе
- SERP Features — фильтр по типам SERP-элементов
- Language — язык (внутри выбранной страны)

**Экспорт (кнопка вверху справа):**
- XLSX, CSV, CSV с ; разделителем
- С группами или без
- Просто скачивает файл — безопасная операция

### Рабочий процесс для нашей задачи

1. Выбрать страну (Brazil или Mexico)
2. Ввести seed-ключ (например "praticar inglês")
3. Нажать Search
4. Применить фильтры если нужно (KD, Volume, Include/Exclude)
5. Просмотреть результаты, при необходимости переключить match type
6. Экспортировать CSV
7. Повторить для следующего seed-ключа

## Keyword Overview

### URL
`https://www.semrush.com/analytics/keywordoverview/`

### Назначение
Детальная информация по конкретному ключевому слову. Метрики, тренды, SERP-анализ.

### Интерфейс
- Поле ввода ключа
- Dropdown страны
- Основные метрики: Volume, Global Volume, KD%, CPC, Intent, Competitive Density
- 12-месячный тренд-график
- Вкладки: Variations, Questions, Clusters
- SERP Analysis — топ-ранжирующиеся домены
- Bulk Analysis — до 100 ключей одновременно

## Organic Research — анализ конкурентов

### URL
`https://www.semrush.com/analytics/organic/overview/`

### Назначение
Посмотреть, по каким ключам ранжируется конкурент, его топ-страницы, трафик.

### Интерфейс
- Поле ввода домена (duolingo.com, talkpal.ai и т.д.)
- Dropdown типа: Root domain / Subdomain / URL / Subfolder
- Dropdown страны/базы данных
- Toggle: Desktop / Mobile

### Вкладки
- Overview — общая картина (ключи, трафик, тренды)
- Positions — таблица всех ключевых слов с позициями
- Position Changes — что выросло/упало
- Competitors — конкурентная карта
- Pages — топ страницы по трафику

### Экспорт
- XLSX, CSV
- Выбор количества строк: 100, 500, 1000, 3000, 10000, 30000, 50000

## Лимиты плана

Плана за $200 нет. Вероятные варианты:
- **Guru** ($249.95/мес): 30K результатов/отчёт, 1500 ключей трекинг, 15 проектов, историч. данные
- **Pro** ($139.95/мес): 10K результатов/отчёт, 500 ключей трекинг, 5 проектов

Экспорт строк:
- Pro: до 10,000
- Guru: до 30,000
- Business: до 50,000

## Важные нюансы интерфейса

- Страна/база выбирается ДО поиска — после поиска сменить можно, но это новый запрос (расходует лимит)
- Фильтры применяются к текущим результатам без нового запроса (не расходуют лимит)
- Группы в левом сайдбаре тоже не расходуют лимит — это фильтрация загруженных данных
- Экспорт скачивает файл в ~/Downloads или в рабочую директорию браузера
- Cookie-баннер может появляться — нажать "Allow all cookies" при первом визите

## Скачанные файлы — экспорт CSV

Chrome работает в Docker-контейнере (pinchtab-chrome) **без volume-маунтов**.
Файлы, скачанные через кнопку Export, остаются ВНУТРИ контейнера и недоступны напрямую.

### Способ 1: JS-перехват через /evaluate (РЕКОМЕНДУЕМЫЙ)

**Полный рабочий процесс для Keyword Magic Tool:**

1. Навигировать на страницу с данными
2. Инжектировать JS-перехватчик загрузок через `/evaluate` (см. docs/pinchtab.md, раздел /evaluate)
3. Кликнуть Export → CSV (стандартные browser.sh click)
4. Прочитать перехваченный URL из `window.__dlLog`
5. Сделать `fetch(url)` внутри браузера — он использует cookies текущей сессии
6. Забрать содержимое порциями через `window.__csvContent.substring()`

Результат: полный чистый CSV от SEMrush (все ключевые слова, все колонки, заголовки).

Автоматизированный скрипт: `seo/sourcechain/export_csv.sh kmt "keyword" us output.csv`

### Способ 2: DOM-скрапинг через /evaluate

Для страниц, где нет кнопки CSV-экспорта (например, Organic Rankings → Positions tab):

1. Навигировать на страницу
2. Через `/evaluate` вызвать JS, который читает `[role=row]` элементы из DOM
3. Собрать CSV из ячеек таблицы
4. Забрать порциями

Автоматизированный скрипт: `seo/sourcechain/export_organic.sh domain.com us output.csv`

Ограничение: только видимая страница (обычно 100 строк). Для полного экспорта нужна пагинация или фильтры.

### Способ 3: browser.sh text + парсинг (устаревший)

`browser.sh text` возвращает конкатенированный текст страницы без структуры.
Парсинг ненадёжен (числа сливаются без разделителей). Использовать только если /evaluate недоступен.

### Способ 4: docker cp (запасной)

```bash
docker exec pinchtab-chrome find / -name "*.csv" -newer /tmp/start_marker 2>/dev/null
docker cp pinchtab-chrome:/path/to/file.csv ./
```
На практике файлы часто не появляются в контейнере. Способ 1 надёжнее.
