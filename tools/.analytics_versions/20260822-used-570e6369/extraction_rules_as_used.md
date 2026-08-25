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


---

# Глосарій

# Glossary — write the output in exactly these terms

## Abbreviations (given by the owner)

- `ор` — орієнтир
- `лс` — лісосмуга (лісополка, лісополоса, полка)
- `півн` — північ · `півд` — південь · `зах` — захід · `сх` — схід
- `н\п` — населений пункт
- `р\н` — район
- `в\с` — військовослужбовець
- `омсбр` — бригада · `мсп` — полк · `мсб` — батальйон · `мср` — рота
- `шг` — штурмова група: будь-яка озброєна група в\с, що пересувається в зоні, КРІМ груп із
  конкретним небойовим завданням (евакуація, логістика тощо)
- `йм` / `ім` — ймовірно, імовірно

## Terms that recur in the corpus

Definitions below are the owner's own — use exactly these expansions.

- `о\с` — особовий склад
- `МТЗ` — матеріально-технічне забезпечення (вантаж, скид, доставка)
- `ТЗ` — транспортний засіб · `МТ` — мототранспорт
- `КНП` — командно-спостережний пункт · `ВУ` — вогневе ураження · `РЕБ` — радіоелектронна боротьба
- `РОВ` — російські окупаційні війська · `СОУ` — сили оборони України · `БпЛА` — безпілотник
- `накопичувач` / `проміжний накопичувач` — місце збору в\с противника, куди їх потроху приводять і
  збирають для подальшого розподілення та переміщення далі
- `укриття` / `бліндаж` — позиція перебування
- `т` перед номером/назвою — точка: `т27`, `т 25`, `т1-2`, `т ЛОШАДЬ`
- `т\н` — телефонний номер
- `300` — поранений · `200` — загиблий
- `ТМ` — важка (протитанкова) міна; буває скидання з коптерів
- `н\в` — невідомо, невідомий, не визначено, не визначений
- `зх` — захід (рівнозначно `зах`)
- `ст мережі` — старший мережі · `гр` — група · `ком` — командир
- `ком склад` — командирський склад · `ст ком склад` / `стар ком склад` — старший командирський
  склад · `мол ком склад` — молодший командирський склад
- `кр` — командир роти · `кв` — командир взводу · `кб` — комбат
- `СП` — спостережний пункт · `КНП` — командно-спостережний пункт
- `«союзники»` — суміжний підрозділ РОВ (у їхньому ж жаргоні), беремо в лапки як у корпусі

## Мова власних назв — НЕ перекладати

Позивні, назви точок і будь-які інші власні назви, зняті з перехоплення, пишуться **російською —
так, як їх вимовляє противник**: `ЛЮТЫЙ`, а не `ЛЮТИЙ`.

Причина не в стилі. Інформація з перехоплення сама по собі не гарантована, і переклад власної назви
додає ще один шар втрати сенсу: змінена назва може перестати збігатися з тим, що вже записано, і
зв'язок між подіями губиться. В архіві подекуди трапляється українізований варіант — це не зразок
для наслідування, з цього моменту не перекладаємо.

Відмінювання позивних і назв точок теж уникаємо. Замість «біля позиції Лютого» — конструкція, де
назва лишається незмінною: `у р\н між позиціями МОЛДОВАН та ЛЮТЫЙ`. Позивний тут читається як
орієнтир на місцевості, і саме так його й треба подавати.

## Розпізнане на слух — спотворення ASR

The input is a transcript of speech, so codes and abbreviations arrive mangled. Restore the term
when the reading is confident; otherwise keep what was heard, in quotes.

- «таемки», «таємки», «те-емки», «тмки», «две таемки» → **ТМ** — важка (протитанкова) міна.
  Трапляється скидання ТМ з коптерів. У рядку: `скидання двох ТМ з БпЛА`, `виявлено ТМ у р\н…`

## Сторони — хто говорить і чия дія

`СОУ` — сили оборони України. `РОВ` — російські окупаційні війська.

The log is written from the СОУ analyst's side, while the voices on the air are, as a rule, РОВ
radio nets. That gap has to be crossed in every line:

- **Whose net it is, is decided per fragment, not assumed.** Read it off the evidence: who is called
  «наши» / «союзники», the unit designations (мсп, омсбр, мсд with Russian numbering), the callsigns,
  the vocabulary. If the fragment genuinely does not show it, do not guess — mark with `ім`.
- **The speakers' «наши» is РОВ.** Their `«союзники»` is an adjacent РОВ unit, never СОУ.
- **The threat they complain about is usually СОУ action, and is named as such:** their «птичка»,
  «сброс», «фипик», «семечки», «прилёт» become `БпЛА СОУ`, `ВУ СОУ`, `скид МТЗ СОУ` in the line.
  The corpus does exactly this: `в\с КУБА- 300, влучання БпЛА СОУ`, `активне ВУ СОУ по укриттю
  накопичування КОЛЯН`, `БпЛА СОУ типу «ждун»`.
- A `300` among the speakers is a РОВ casualty; the side that caused it is named only when the
  fragment supports it.

## Enemy voice codes seen so far (decode only when the network matches)

Спільні для кількох мереж:

- `55`, `12` — прийнято, зрозуміло; в деяких мережах `55` — «стан справ»
- `нолик` — БпЛА (ім DJI Mavic) · `яндекс` — БпЛА доставки МТЗ
- `412` — гексакоптер · `414` — БпЛА, що планерує · `415` — ударний БпЛА СОУ
- `десятка`, `пятнашка`, `пятак` — коптер-камікадзе
- `11` — БпЛА зі скиданням · `22` — БпЛА типу «Вампір» · `33` — ім гексакоптер
- `44`, `54`, `55`, `555`, `962`, `9 6 2`, `05`, `450` — запит/доповідь про стан справ
- `82` — продовження руху · `21` — рух, переміщення о\с або ТЗ · `1 10` — канал зв'язку
- `30` — «300» (поранений) · `молоко` — паливо
- `окно` — укриття, бліндаж · `пятак` (у 114 мсп) — позиція формування шг
- `пушистики` — о\с ім будь-яких підрозділів «союзники»
- `платье` — пончо, халат · `марс` — канал №3 н\в р\с 9 мср

Коли код невідомий або мережа не збігається — залишати як прозвучало, у лапках.


---

