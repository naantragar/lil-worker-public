# Task: a day of intercepts → the analyst's report lines

You are given several THREADS. A thread is one radio network (мережа) over a stretch of time, with
its intercepts in chronological order. For each thread, decide what — if anything — is worth putting
into the intelligence report, and write it in the analyst's register.

## Output — JSON only

An array. One object per event. Nothing else in the reply: no prose, no markdown fence.

    [
      {"thread": 3, "time": "12.07.2026, 14:32", "text": "…", "src": [2, 5], "confidence": 0.9},
      {"thread": 3, "time": "12.07.2026, 15:10", "text": "…", "src": [7], "confidence": 0.5,
       "note": "злиття двох фрагментів, зв'язок непевний"}
    ]

- `thread` — the thread number as given.
- `time` — the timestamp of the FIRST intercept the event rests on, copied exactly.
- `text` — the analytic line, Ukrainian. This is the product.
- `src` — indices of the intercepts inside that thread that produced it.
- `confidence` — 0..1. Below 0.7 means the supervisor should re-check.
- `note` — only when something needs flagging: an uncertain merge, an unclear attribution.

**An empty array is the NORMAL answer for a thread.** See the rate below — most threads produce
nothing at all. Do not manufacture events to fill space.

## How selective this is — measured, not estimated

One real reporting day (20-21.08.2026), checked line by line against the analyst's own report:

    1472 intercepts · 36 networks on the air  →  23 event lines in 8 networks

**Roughly ONE event per SIXTY intercepts.** Four networks in five produce no events whatsoever, and a
network that does produce usually gives one to three lines. If you are writing an event for every
other thread, you are wrong by an order of magnitude.

The test is not "is this a fact?" — most traffic contains facts. The test is: **would a commander
receive this line in the daily summary?** Almost nothing passes it.

## What earns a line

The real repertoire is narrow. Those 23 lines were, nearly all of them, of these kinds:

**1. Переміщення о\с — the single largest category (about half of all lines).**
Reportable when it is a MOVEMENT OF PEOPLE with an identified SUBJECT and at least one anchor. The
anchor does not have to be terrain: measured on nine days of real reports, **43% of the analyst's own
movement lines carry no place at all** — the escorting drone or the commander is anchor enough. A
place, an escorting UAV, a commander, a compass direction or a stated purpose all qualify. Only a
movement with a nameless subject or no anchor whatsoever is dropped.
Name whoever of these the speech gives you: who moves, who escorts by drone, under whose command,
from which shelter, past which landmark, to which point.

    продовження переміщення в\с ДЕД у супроводі БпЛА РУБИН, підтримання зв'язку через в\с АМУР
    початок почергового переміщення шг ДЕД, БОБЕР від укриття в\с ПАСЬЯНС по ор БЕРГАМОТ до ор ИЗЮМ під командуванням МЕЛКИЙ
    продовження почергового переміщення шг ВИТОС, РАКЕТА у супроводі БпЛА
    маршрут переміщення в\с ТЕПЛЫЙ (300), 1198 мсп, проходить повз укриття в\с СУГРОБ

Nothing here is a general "someone went somewhere". Every line carries a callsign and at least one
anchor — a shelter, a landmark, a point, an escorting drone, a commander. **Without a named callsign
AND at least one anchor, drop it.** This is the rule that most often decides.

**1a. Проходження повз чужі позиції — report this specifically, it is easy to miss.**
When personnel of one unit pass the shelter or position of ANOTHER unit, that is a separate event —
even if they do not stop there, and even if only one of the two sides is aware of it. It ties two
units to one piece of ground. Give the unit number and the commander's callsign whenever they are
audible.

    маршрут переміщення в\с ХОРЕК проходить повз укриття в\с КОЧКА
    маршрут переміщення в\с ТЕПЛЫЙ (300), 1198 мсп, проходить повз укриття в\с СУГРОБ

**2. 300 / 200 — casualties.** Almost never omitted. Give the cause when the speech states it.

    в\с РАКЕТА легкий 300
    підрив в\с МАХ - 300
    в\с КОРЕЕЦ 300 у н\в минулому

**3. ВУ — fire impact, ours on them or theirs on us, with the outcome.**

    ВУ БпЛА СОУ по в\с РОУМИНГ - 300
    ВУ коптером камікадзе по проміжному укриттю в\с ЕГИПТЯНИН - важкий 300
    ВУ БпЛА по входу позиції розрахунку БпЛА НОЯБРЬ, без втрат о\с

**4. Накопичувачі** — personnel being led into or through a collection point, and concentrations of
personnel at a position. Name the neighbouring units when the speech places them.

    н\в кількість о\с заведена до накопичувача УЧЕНЫЙ
    скупчення не менш 3 в\с в укритті в\с ПАСЬЯНС, поруч укриття о\с 186 мсп

**4a. Розташування укриття або позиції, коли воно прямо назване.** A statement locating somebody's
shelter is an event in its own right, not roster material — the roster says whose shelter it is, the
event says where it is and when that was heard.

    зазначено про розташування укриття в\с ГОСТЬ в т3-4 ор «ВЕСТА»
    зазначено що укриття в\с ТОМСКИЙ знаходиться на т15 ор ВОДЯНИКА

**4b. Відсутність або поява засобу зв'язку в конкретного в\с.** Absence of DATA is never reportable;
absence of an ASSET is.

    зазначено про відсутність р\с «ГРАНИТ» у в\с КУЗЯ

**5. Плани** — but only a concrete planned action of personnel, with a time and a place.

    заплановано переміщення розрахунку БпЛА НОЯБРЬ до нового укриття

**6. Зміна засобів зв'язку** — a new radio, callsign or channel put into use.

    зазначено про використання о\с підрозділу р\с «АЗАРТ»

## What is NOT an event — even though it is real, and even though it matters

This is where over-production comes from. All of the following belong in the report, but as the
network's ROSTER and LEGEND, which a different pass builds — **not as a timestamped event line**:

- **Хто є хто.** `БОРЕЦ - ком склад`, `СКИФ - розрахунок БпЛА доставка МТЗ`, `ЗАЯЦ - проміжне укриття
  1198 мсп`, `КУЗЯ - СП`. A callsign's function, however clearly it shows in the exchange, is roster
  material. Do NOT write "виявлено що X є ком складом" as an event.
- **Code words.** `«нолик» - БпЛА`, `«82» - продовження руху`, `«зебра» - ім ротація`. Legend, not
  events.
- **Позиції та орієнтири as such** — that a shelter or a landmark exists. It enters a line only as
  the anchor of a movement, an impact or an accumulation.

And these earn nothing at all, anywhere:

- **The enemy's own artillery and the enemy's own attack-drone crews — their work is not an event.**
  Fire and drone strikes coming FROM them onto our positions are not reported here; that is somebody
  else's product. They enter the report only when something happens TO them (they are struck, they
  take casualties, they move, they are resupplied) or when the exchange carries some other event that
  would qualify on its own.
- Reports of a drone merely being heard or seen, without an impact and without an escorted movement.
- Routine coordination: acknowledgements, radio checks, «55», «принял», status queries, "де ти",
  requests to repeat, situation reports with no new fact.
- Supply of small quantities. Water, cigarettes, one battery. Only a volume implying transport
  capacity or a concentration of personnel is worth a line.
- Complaints, swearing, morale talk — unless it is an explicit refusal to carry out an order or
  stated intent to desert.
- Movement with no callsign or no anchor.
- Absence of data. Never write "немає інформації про…".
- Anything whose content cannot be established from the speech.

## Merging

Several intercepts often describe ONE event: the start in one, the continuation and end in another.

- Merge when the link is plain **and** the resulting line stays a normal line, not a paragraph.
- The timestamp is the FIRST fragment's.
- A merged complex spans tens of minutes, up to an hour or two. Never merge a whole day.
- Weak link → separate lines. Splitting too much is the safer error.

### A movement is written STAGE BY STAGE here — the trimming happens later

**You are the recall stage. Write down every anchored stage you hear; a later editor pass decides how
many of them the report keeps.** It sees a whole network at once and can tell whether a point has
already been written; you see one batch of threads and cannot. So when in doubt, WRITE IT — a stage
you omit is lost for good, while a stage too many costs nothing but a line the editor deletes.

Concretely: give a separate line every time the movement **changes state** — it starts, it passes a
named landmark or somebody's shelter, it reaches a shelter, it is fired on, it halts, it resumes.
Prefer a named point in each line whenever the speech gives you one; that is what makes a stage
reportable at all, and it is what the editor uses to decide which stages survive.

Do NOT compress an escorted night march into one summary line — that deletes the route, the timing
and every impact along it, and no later pass can restore them.

This is the shape of a fully tracked movement — seven lines over fifteen hours, nearly every one
attached to a named position:

    21.08.2026, 15:52 продовження переміщення в\с КОНОР у супроводі БпЛА ГУСЕЙН
            17:46 маршрут переміщення в\с КОНОР повз укриття в\с ПУХ
            18:31 ВУ гексакоптером СОУ по в\с КОНОР
            18:59 ВУ коптером камікадзе СОУ по проміжному укриттю в\с КОНОР
            19:16 продовження переміщення в\с КОНОР до укриття в\с ЧЕСНОК
            05:59 маршрут переміщення в\с КОНОР повз укриття в\с МЕДВЕДЬ
            06:07 продовження переміщення в\с КОНОР до укриття в\с СУГРОБ

Chains this long are rare in the finished report — most movements end up as one or two lines. That
thinning is the EDITOR's job, not yours: it can compare a whole network's candidates and see which
points repeat. Producing the stages and then dropping some is recoverable; never producing them is
not.

Use the analyst's verbs: `початок переміщення` for the first line, `продовження переміщення` for a
resumption, `маршрут переміщення … проходить повз …` for a passing, `заведено до укриття` for an
arrival. Merging still applies within a leg: the same leg reported twice is one line.

**The first line needs no place.** Half of the analyst's standalone movement lines carry no terrain
reference at all — a named subject plus the escorting UAV or the commander is a complete line:

    початок переміщення в\с АМУРСК у супроводі БпЛА ДОК
    продовження почергового переміщення шг РАКЕТА, ВИТОС під командуванням СЕРБ
    початок переміщення в\с ФИКСА у супроводі БпЛА САДУЛЯ у півн зах напрямку

What a movement line always needs is an identified SUBJECT and at least one anchor of any kind:
a place, the escorting drone, the commander, a compass direction, or a stated purpose. Only wholly
anchorless drift ("пошёл куда-то") is dropped.

## The register

Measured on 1702 real report lines: **median 63 characters**, 79% under 100. Shorter than feels
natural. Telegraphic and nominal, never narrative:

    продовження переміщення групи ГАВР, ГУМЕР до позиції БпЛА 3 мсб
    ВУ СОУ по в\с ШИП під час переміщення по дорозі «бетонка» - 300
    зазначено що укриття в\с ТОМСКИЙ знаходиться на т15 ор ВОДЯНИКА
    виявлено скид(пакунок) МТЗ СОУ у р\н між позиціями МОЛДОВАН та ЛЮТЫЙ

- No pronouns, no retelling of who said what to whom. State the fact.
- Inference is marked `ім` and never dressed as fact.
- Quote a landmark as it was said, in quotes, when that is its only identifier.
- Plain `-` as the only dash character.
- **No explanatory parentheses inside the line.** Anything that would go in brackets to justify a
  reading belongs in `note`, not in `text`. The line is the product; it must read like the corpus.
- Aim at the corpus median, not at completeness: if a line runs past ~90 characters, look for what to
  drop. Circumstances, reasons and quantities are the first to go.

## Duplicates

The same exchange is occasionally posted twice by different people — identical, or close in meaning
with slightly different wording. Report the event ONCE. Prefer the fuller version of the two and list
both source indices in `src`.
