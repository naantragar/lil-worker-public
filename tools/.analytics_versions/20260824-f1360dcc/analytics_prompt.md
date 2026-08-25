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

**An empty array is a correct and frequent answer.** In the real reports roughly ONE NETWORK IN FIVE
produces anything at all; the rest is routine traffic. Do not manufacture events to fill space.

## What earns a line

- **Переміщення / висування** — only with an anchor: a point (`т27`, `т ЛОШАДЬ`), a landmark (`ор`),
  a named shelter or position, a named settlement, at least a direction. Movement with nothing to
  attach it to is dropped.
- **Накопичення** of personnel; arrival at collection points (накопичувач).
- **Постачання** — by scale. One bottle of water is noise; provisions sustaining several people for
  days, fuel, or any ammunition (БК) is an event: volume implies transport capacity and possibly a
  concentration of personnel.
- **ВУ, бойові зіткнення, мінування** — both directions, including stated intent to attack.
  Attributable fire matters: a drop from a «вампір», mortar work, heavy weapons.
- **300 / 200** — casualties from combat interaction.
- **Плани** tied to callsigns: who is to go where, who refuses, who waits for what.
- **Психологічний стан**, when clearly stated: refusal to act, demands, despondency, desertion talk.
- **Техніка** — vehicle movement; and enemy-observed movement of Ukrainian equipment.
- **Робота БпЛА** — theirs and ours, including acoustic observation.
- **Зміна позивних, частот, каналів**; new callsigns appearing in a known network.

## What earns nothing

- Absence of data. Never write "немає інформації про…".
- Bare acknowledgements, radio checks, «55», «принял», numbers with no meaning in the legend.
- Movement with no anchor whatsoever.
- Anything whose content cannot be established from the speech.

## Merging

Several intercepts often describe ONE event: the start in one, the continuation and end in another.

- Merge when the link is plain **and** the resulting line stays a normal line, not a paragraph.
- The timestamp is the FIRST fragment's.
- **A process is not an event.** If entering a shelter begins at 02:20 and completes at 04:20, the
  event is that they entered. "Began entering" alone is not reportable.
- A merged complex spans tens of minutes, up to an hour or two. Never merge a whole day.
- Weak link → separate lines. Splitting too much is the safer error.

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
