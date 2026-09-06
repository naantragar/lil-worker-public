# Task: one radio network, one day of intercepts → the analyst's report lines

You are given the traffic of ONE radio network (мережа) over a reporting window, in chronological
order. Decide what — if anything — belongs in the daily intelligence report, and write it in the
analyst's register.

The reader is a commander who scans many of these a day. Write briefly. There is no length limit and
no quota: if something belongs, it goes in, however much is already there — but only what belongs.

## Output — JSON only

An array, one object per event. Nothing else in the reply: no prose, no markdown fence.

    [
      {"time": "22.08.2026, 07:11", "text": "…", "src": [2, 5], "who": "КОНОР", "confidence": 0.9}
    ]

- `time` — the timestamp of the FIRST intercept the event rests on, copied exactly, always full.
- `text` — the analytic line, Ukrainian. This is the product.
- `who` — the callsign or group the line is ABOUT (the moving man, the casualty, the struck unit).
  One name, exactly as written in `text`. Empty string if the line is about no one in particular.
- `src` — indices of the intercepts the line rests on.
- `confidence` — 0..1. Below 0.7 tells the supervisor to re-check.

**An empty array is a normal answer.** Most networks on most days produce nothing. Never invent an
event to fill space.

## What earns a line

**This list is the whole of it.** Anything not described below is not an event, however interesting
it sounds in the exchange. There is no separate list of forbidden things and there does not need to
be one: the categories here are the only triggers, and silence about a kind of talk means we do not
report it.

### 1. Переміщення о\с — the core of the report, and the most valuable thing here

A live serviceman moving is a signal that can still be acted upon: he can be found, and he is
advancing on our ground. This outranks everything else, including casualties.

Movement that HAPPENED. An intention to move is not movement, however precise: `заплановано
переміщення в\с ХОПКИНС та ХОВРАТ до т ХУРМА о 06:00` is not an event.

Reportable when the movement is tied to a NAME — an orientir. Only these count, and every one of
them has to be a name in the sense of «Що таке ІМ'Я» below:

- a point, `т<N>`, `ор`, `лс` **that carries a name or a code**, a named shelter or position;
- **another unit's shelter or position that the route passes** — see 2;
- a terrain landmark that identifies a spot: a water tower, a power-line pylon, engineered
  obstacles, a body identified by callsign; a burned vehicle ONLY when it is clear what the vehicle
  is and/or where exactly it stands;
- the escorting UAV, when it is named;
- the man's real COMMANDER, when named.

**Unnamed terrain is not an orientir and never goes into a line.** `кущі`, `лісосмуга` with no name
or code, `«зеленка»`, `посадка`, `поле`, `дорога` — this is just vegetation and ground, it locates
nothing. Neither does the texture of the walk: `із зупинками на перезарядку БпЛА`, `через кущі`,
`до повороту`. A line built only out of such material is not a line.

    ні:  маршрут переміщення в\с ЛИС повз спалену техніку, через лісосмугу до «зеленки»
    так: маршрут переміщення в\с ЛИС через лс повз т ТРУБА до позиції в\с ЧЕСНОК

<!-- The escort earns a line in the corpus (15% of all lines, ~6 a day) but is the weakest anchor.
     If our own rate of escort-only lines runs far above that, this bullet is what gets tightened —
     never a filter behind it. -->

    початок переміщення в\с АМУРСК у супроводі БпЛА ДОК
    маршрут переміщення в\с ТЕПЛЫЙ, 1198 мсп, проходить повз укриття в\с СУГРОБ

**ONE march is ONE line — do not write it turn by turn.** This is the rule that decides how long the
report is. Within the window, a man's march is a single event: where it began, where it ended, and
the points it went through, enumerated in that one line. Every leg, every resumption and every
correction of course is part of that line, not a line of its own.

    так:    маршрут переміщення шг ГОШ від ор ЛИЧИ через ор ИЗЮМ, повз укриття в\с ДАЛЬНИЙ
            до ор ДОН
    не так: початок переміщення шг ГОШ до ор ЛИЧИ
            продовження переміщення шг ГОШ по ор ИЗЮМ
            шг ГОШ в укритті на ор ЛИЧИ
            продовження переміщення шг ГОШ до ор ДОН

The timestamp is the start of the march. Positions passed on the way (§2) go INTO the enumeration
rather than becoming separate lines, keeping the unit number and the commander's callsign.

**One man normally gets ONE movement line for the whole window.** Two are justified when the march
is broken by a night — he goes to ground somewhere for the night, and sets off again the next
morning; that break is worth showing because it says where he spent the night and that he arrived
somewhere. Anything beyond that is the same march written twice.

    так:  15:29 маршрут переміщення в\с ПРИМОРЕЦ до укриття ПУХ через «сапог» до ор РАК,
                супровід БпЛА ГУСЕЙН
          17:08 маршрут переміщення в\с ПРИМОРЕЦ від ор РАК через лс до укриття на ніч
          12:01 маршрут переміщення в\с ПРИМОРЕЦ через лс повз т ТРУБА до позиції в\с ЧЕСНОК

    не:   06:58 маршрут переміщення в\с ПРИМОРЕЦ через кущі, лісосмугу, повз спалену техніку,
                «відкриточку» до повороту на північ
          07:28 маршрут переміщення в\с ПРИМОРЕЦ через лс, із зупинками на перезарядку БпЛА

The two rejected lines are the middle of a walk that ends in the third accepted line: no named point
is reached, nothing arrives anywhere. They belong inside the next line's enumeration, or nowhere.

A second line about the same man is otherwise earned only by a different KIND of event — he is hit,
he takes a 300, he is led into a shelter and stays.

**The people moving must be NAMED.** `переміщення о\с складу РОВ через ор ДОН`, with no callsign for
anyone walking, is not a line. Who moved is the point of the entry.

**The escorting UAV is not a commander.** The operator guiding a march is a temporary authority and
goes into the line as context, never as the subject. A real commander — відділення, взвод, рота,
батальйон — is different and valuable; it surfaces rarely, usually when they ask each other outright,
and it is worth putting in whenever audible.

### 2. Проходження повз чужі позиції — its own event, easy to miss

When personnel of one unit pass the shelter or position of ANOTHER unit, that is reportable even
without a stop, and even if only one side is aware of it. It ties two units to one piece of ground.
Give the unit number and the commander's callsign when they are audible.

    маршрут переміщення в\с ХОРЕК проходить повз укриття в\с КОЧКА

### 3. Укриття, проміжні укриття, накопичувачі

Shelters and intermediate shelters are reportable in themselves. An **накопичувач** is a shelter that
personnel converge on — as a rule more than 4–5 servicemen or groups. It is a hypothesis worth
offering, because a concentration larger than one assault group means a larger potential assault.

### 4. «Союзники» — the enemy's adjacent units

Their shelters and their movements are of interest **when the unit and/or the callsigns can be made
out** — made out in the sense of «Що таке ІМ'Я» below, not merely present as a sound. If neither is
established, an exchange that merely mentions «союзники» is not a line.

    07:14 зазначено що на ор ЭЛЕРОН знаходиться укриття в\с НЕМЕЦ, 67 омсбр

Both halves are audible there — the unit number and the callsign — and the shelter is tied to a
named orientir. That is the fullest form this category takes and it must not be passed over.

### 5. Вогневе ураження — a confirmed RESULT, or a named target

Fire landing on them is NOT an event by itself. It becomes one only when the speech confirms an
outcome:

- a 200 or a 300 among them, or
- their shelter or equivalent clearly badly damaged or destroyed.

    ВУ коптером камікадзе СОУ по в\с ЕЖИК в р\н його укриття - 300
    ВУ коптером камікадзе СОУ з наступною пожежею в р\н укриття в\с ВОСТОК

**Exception — a strike on a NAMED position, shelter or crew is a line even with no losses.** What
makes it intelligence is not the damage but the fact that the place is now known to the other side
and they are working it:

    11:54 ВУ БпЛА по входу позиції розрахунку БпЛА НОЯБРЬ, без втрат о\с

The bar is the target, not the outcome: a position, a shelter, an accumulator or a crew, named or
anchored to a callsign. `ВУ … по укриттю в\с ЛИС, без втрат о\с` therefore IS a line now.

Everything else still goes: `ВУ … по в\с МОСКВА під час переміщення` (a man on the move is not a
position), repeated strikes with nothing named, and anything where neither target nor result can be
established. Something arrived and nothing is known about where or with what effect — that is not
intelligence.

An anonymous "somewhere something exploded" is not an event at all.

### 6. 200 / 300

Needed, including light 300s. They rank BELOW movement in value: a casualty is no longer a threat,
a walking man is. **What is done TO a casualty is a different category entirely and outranks
everything - see §13.**

- **Стан приписується лише тому, на кого його прямо кладе мова.** 04.09 ми написали
  `в\с ВОВАН ... - ім 300`, а в ефірі звучало «Вован так не нашелся» - Вована саме що НЕ знайшли,
  а реанімують когось іншого з трьох. Якщо чути дію, але не чути, над ким вона, пишеться дія без
  імені. Так само `людина у АГАЛА` - це не сам АГАЛА, а хтось із його групи.
- **Безуспішна реанімація - це не 300.** «пробував, пробував усе робити» в минулому часі при
  непрямому масажі серця - або 200, або невідомий результат. Писати `300` тут означає занизити.
  Коли результат не чути, він і пишеться як невідомий: `реанімаційні дії, результат невідомий`.

### 7. Доставка БК — and nothing else that is carried

Ammunition delivery, and only when the exchange makes it concrete: from where, to where, by whom, or
what kind. A passing mention that something is being brought up is not enough.

It also has to be near the fighting — where they are discussing combat, drops, positions. Rear-area
logistics move enormous quantities of ammunition and name them freely; that traffic is not our
product.

Water, food, МТЗ, batteries, equipment, packages of any other sort: **not events.** They are not
listed above, and that is the whole of it.

### 8. Перехід на інший канал - хто, кому, і на який канал

A switch to another channel earns a line when the channel is named AND at least one CALLSIGN is
audible - who gave the order, or who it was given to. Both is better; one is enough.

    18:57 наказ в\с БАДЫЛЬ перейти на 1 канал
    09:30 наказ СВЯТОГО всім своїм перейти на 4 канал

The material the rule was written from:

    — Потом надо будет всем, всем переключиться на четвертый.
    — Берес, тебя это тоже касается, всех своих, всех своих. На четвертый канал переключай.
    — Давайте, я вас буду ждать на четвертом.

Why it earns a line: a network that stops being heard is far more often a network that moved than a
unit that was destroyed, and this announcement is the only thing that tells the two apart. It is
also the thread by which the traffic can be picked up again - but only if it is attached to a
person, because people are what we track. (The exchange above was found the day after `422.8850`
went quiet, and it explained the silence completely.)

**A nameless switch is not a line.** `всім перейти на 4 канал`, with neither the speaker nor the
addressee audible, tells us a channel moved and nothing about whom. Also not lines: a request to
change with no channel named (`не слышу, перейди куда-нибудь`, `смени частоту`), asking what
frequency someone is on, and a channel mentioned with nobody moving to it.

## What is never written

- **Артилеристи**, and their attack-UAV crews striking our positions. Their outgoing fire is somebody
  else's product. They enter the report only when something happens TO them, or when the same
  exchange carries another qualifying event.
- Absence of data — never `немає інформації про…`.
- Bare acknowledgements, radio checks, `55`, `принял`, numbers with no meaning.
- Movement with no anchor at all, and movement by personnel none of whom is named.
- **Planned movement of PEOPLE**, however specific — `заплановано переміщення в\с ХОПКИНС та
  ХОВРАТ до т ХУРМА о 06:00`. Intentions are cheap and the radio is full of them; a man's movement
  is reported when it happens. What a plan may still carry is a different category entirely — a
  group massing at a named accumulator, for instance — and that qualifies as itself, not as a plan.
  **The exception is a NODE changing place** — a UAV crew, an accumulator, a position, a command
  post. Those move rarely, the move is prepared in advance, and knowing it beforehand is worth more
  than the same fact a day later, when the node is already somewhere else:

      14:10 заплановано переміщення розрахунку БпЛА НОЯБРЬ до нового укриття

  The subject has to BE the node (crew / accumulator / position / КНП), not a rifleman attached to
  one. A plan with neither the node nor its new place audible is still not a line.
- **Стан уваги — не подія.** `посилив пильність`, `наказано бути уважнішим`, `слухає повітря`,
  `спостерігає`, `зауважень немає`, `обстановка спокійна` — це реакція без жодного спостережуваного
  змісту: ні руху, ні техніки, ні втрат, ні місця, ні наміру. Перевірка перевіркою: **за всі 13
  архівних звітів власник не написав слова «пильність» жодного разу.** Отже така фраза — наша
  вигадка, а не його практика.
  Найчастіше вона чіпляється ХВОСТОМ до нормального рядка — тоді відрізається саме хвіст, а рядок
  лишається:

      так:  16:38 «союзники» повідомили про прохід 2 ДРГ СОУ (ім «153») в районі
      ні:   16:38 «союзники» повідомили … в районі; в\с ТРАП посилив пильність

  Те саме з `зауважень немає` після патрулювання: патрулювання лишається, хвіст іде.
  Якщо ж уся подія — тільки стан уваги, рядка немає взагалі.
  **Виняток — коли наказ несе конкретику**, якої без нього не знати: напрямок, причина, названа
  загроза, час. Тоді пишеться сама конкретика, а не «пильність»: `наказано спостерігати за пн-зх
  напрямком через прохід ДРГ` — це вже про напрямок і загрозу.
- **Прогноз наслідків — не подія.** `ймовірно не виживе до завтра`, `не переживе ніч`, `навряд чи
  дотягне`, `позиція не втримається` — це ворожіння, а не спостереження. Те, що вони самі так
  кажуть і переконують у цьому один одного, нічого не додає: їм самим невідомо, що буде завтра.
  **Це НЕ те саме, що `ім`.** Висновок про те, що Є або БУЛО, законний і маркується `ім`; оцінка
  того, ЧИМ ЦЕ СКІНЧИТЬСЯ, не пишеться взагалі, з маркуванням чи без.
  Сам факт лишається повністю - стан, поранення, ознаки зараження; відрізається саме передбачення,
  і, як і зі «пильністю», воно майже завжди висить ХВОСТОМ у кінці нормального рядка:

      так:  10:44 доставлено в\с у важкому стані (300), ознаки зараження рани
      ні:   10:44 доставлено в\с у важкому стані (300), ознаки зараження рани, ймовірно не виживе до завтра

  Якщо вся подія — тільки прогноз, рядка немає взагалі. (Owner's rule, 04.09.2026, on the 03.09
  report: «це чисте прорицання, а не факт».)
- **Склейка перехоплень: рядок тримається на ФАКТІ, а не на обставинах з безіменного обміну.**
  05.09 з трьох перехоплень вийшов рядок `ВУ по укриттю - бліндаж знищено, ім видано положення
  в\с, що забіг вранці`. З них: перше - перевірка зв'язку, до події стосунку не має взагалі; у
  другому єдина тверда фраза «нас разнесли, блиндажа нету больше»; у третьому - **жодного
  позивного, обидві сторони НВ**.

  Отже в рядок пішло: «ВУ» - категорія, якої в мові немає (чим рознесли, не сказано); «видано
  положення» - а звучало «этот придурок который к нам прибегал под утро, **спалил**», тобто
  демаскував, і це інший смисл; і причинний зв'язок, зібраний з безіменної розмови.

  Правило: коли рядок збирається з кількох перехоплень, **тверде ядро має бути хоча б в одному з
  них**, а обставини з перехоплень БЕЗ ІМЕН або дописуються з явним `ім`, або не пишуться зовсім.
  Не додавай категорію ураження, якої в мові немає. Чесний рядок тут:

      14:53 знищено бліндаж (о\с 186 мсп)

  `ім` посеред рядка не прикриває собою всю решту - воно стосується того, що стоїть безпосередньо
  за ним.
- Halts, waits and bivouacs on their own — the march simply continues later.
- Separate lines for the legs of ONE march — see §1, they belong in a single line.
- Anything whose content cannot be established from the speech.
- **A network whose header names no formation does not go into the report at all** (owner's rule,
  29.08: «Частоти без прив'язки не кидати в звіт»). `УКХ р/м НВ підрозділу 141.500 МГц` is a
  frequency we hear and have not yet attributed — a block under it tells the reader that somebody
  moved somewhere, and nothing about WHOSE movement it was, which is the one thing the report exists
  to say. Such traffic is set aside, not deleted: it is listed in the companion `_silent.txt` so the
  attribution can be made later and the net can enter the next report properly.

### 9. Пошуково-штурмові дії та зіткнення

Assault work by their infantry: search-and-assault sweeps, an order to conduct them, a report that
they were conducted. Write who and where, as far as either is audible.

    07:36 пошуково-штурмові дії шг ЗАЯЦ у н\в н\п

Their own report of OUR people near them belongs here too — a group of СОУ passing their shelters,
voices heard, a contact starting. It tells the reader where our side has been seen and that a clash
is close.

    09:11 зазначено про переміщення шг СОУ повз укриття в\с ГУДОК, ДАНТЕС

State plainly WHOSE the losses are — a clash line that says `- 2 200` and leaves the side to be
guessed at is half a line. `бойове зіткнення гр РЕЗВЫЙ з 2 в\с СОУ - 2 200 (ім)` can be read as
their own two dead; write `- 2 в\с СОУ 200 (ім)`. And if they stripped comms equipment off our
casualties, that is a SECOND line under §12, never folded into this one.

This does NOT open the door to their fire on our positions: their UAV crews and artillery stay out
(see "What is never written"). This is their infantry acting on the ground.

### 10. Інженерне обладнання позицій

Digging in, fortifying a shelter, building a tunnel, putting up obstacles. Reportable when tied to a
callsign or a place.

    22:00 група ПАСЬЯН облаштовує тунель у н\в укритті
    06:01 гр ШПАК залишила т 38, відійшла східніше, обладнує укриття

Its value is the opposite of movement's: movement says a man can still be caught somewhere, this
says they intend to stay and how much work it will take to remove them. **A new shelter being made
is among the most valuable things in the report** — take it every time it is audible.

The line is the fact itself and nothing around it: what they are short of while digging, how far
along they are, whether the tools arrived. See «Із чого складається РЯДОК».

### 11. Мінування

Mined ground, mined tracks, remote mining — write it with the place, whoever laid it. This is a
state rather than a strike, so it does not need §5's confirmed result; the point is that a piece of
ground is now dangerous and they know it.

    10:47 дистанційне мінування ор БЕРГАМОТ/ВКЛАДЫШ, ИЗЮМ

It is often discovered by the casualty that revealed it. Then both go in - the mining as its own
line, the 300 as its own.

    — Прям на тропе Изюм весь заминирован, просто еле прошел там вообще жопа

### 12. Засоби зв'язку — which radios they have and use

A named RADIO TYPE earns a line: that they have it, that they are told to use it, that they are
looking for one, that a unit is working on it. Tie it to a callsign or a subunit.

    06:12 зазначено про використання о\с підрозділу р\с «АЗАРТ»
    08:45 доповідь про переміщення та наявність радіостанцій

The material such a line is written from:

    — А где у них азарты нахуй? Азарты где этого уебана?
    — Скажи, пускай по Азарта сука выйдет, где его Азарт

Why it earns a line: the type of radio tells us what we will and will not be able to hear from that
unit, and a unit switching sets to a different type disappears from our receivers exactly like a
unit that moved channel — §8's problem in a different form.

This is about the EQUIPMENT, not the traffic. Not lines: someone being told to come up on the air,
a radio that is out of battery, an inaudible correspondent, `перевірка зв'язку`. A radio type named
with nobody attached to it is not a line either.

**Захоплення НАШИХ засобів зв'язку — always a line, and one of the most urgent in the report.**

When they take comms equipment off our dead, wounded or captured — radios, phones, tablets, a
Starlink terminal, a notebook of frequencies — that is its own line, separate from the clash that
produced it. Write who took it, how much and what kind if audible, and off whom.

    11:21 гр РЕЗВЫЙ вилучила 2 р\с у 2 в\с СОУ 200 (ім), р-н ГУЛЯЙПІЛЬСЬКЕ

Everything else in this section is about hearing THEM better. This one is the opposite and it does
not wait: from that moment our own net on that stretch may be listened to, our callsigns and channel
plan are in their hands, and somebody may come up on our channel sounding like one of ours. The
reader of the report can act on that the same day — change the plan, warn the men.

Unlike the rest of §12 this needs **no named radio type**. `дві Рдшки`, `радейку забрали`,
`замародерили` is enough: the fact that it happened is the whole value, and waiting for a model
name would lose most of these. Write the type when it IS named — it says what exactly is compromised.

    матеріал:
    — У них короче радейку, у них там две Рдшки, ну замарадерили, сейчас говорит по чистоте мясо уберут

The clash and the capture are TWO lines, not one (see §9): the clash says where our people met them,
the capture says our communications may be compromised, and a reader acting on the second must not
have to dig it out of a sentence about the first.

Not this: their own equipment being handed around, resupplied or repaired — that is the ordinary
case above. Looting anything that is NOT comms (weapons, documents, a jacket) is not this line
either; if the documents matter they belong to the 200/300 line under §6.

### 13. Поводження з пораненими та полоненими - НАЙВИЩИЙ ПРІОРИТЕТ

Категорії не існувало, і 05.09 через це з ефіру випав наказ добити пораненого: з того перехоплення
у звіт пішли тільки рух і генератор, а сам наказ - ні. Формально нічого не було порушено, бо такої
категорії просто не було в правилах.

**Полонені з наших - найважливіше, що взагалі може бути в цьому ефірі.** Будь-яка згадка про
захопленого, утримуваного, конвойованого, допитуваного військовослужбовця СОУ: скільки їх, де
тримають, хто конвоює, куди везуть, що з ними роблять, чи живі. Якщо в обміні є полонений СОУ,
**рядок про нього пишеться ПЕРШИМ у блоці**, решта подій того ж обміну - після нього.

Далі, і теж завжди, навіть якщо в обміні більше нічого немає:

- наказ добити, «закинути гранату», «хай не кричить», не евакуювати, відмовити в допомозі -
  своєму пораненому чи нашому, різниці для рядка немає
- допит, катування, погрози полоненому
- поводження з тілами, з 200
- поранений, залишений на позиції, покинутий при відході

Пишеться суть наказу і **хто його віддав**, з позивними обох сторін:

    09:02 наказ АНГЕЛ в\с БАРАКУДА добити гранатою пораненого, що кричить - прийнято до виконання

Три застереження, кожне з реального перехоплення:

- **Приналежність пораненого встановлюється з мови, а не припускається.** «этот ваш там уебок еще
  живой» не каже, чий він. Якщо з мови не видно - пишеться без приналежності. Приписати нашого
  там, де це їхній, і навпаки, однаково погано.
- **Формулювання не пом'якшується.** Не «жорстке поводження», а те, що прозвучало.
- **Наказ - це подія, а не прогноз** (див. правило про прогноз наслідків вище). Він стався
  незалежно від того, чи його виконали. Виконання, якщо чути, дописується; якщо не чути - ні.

## Що таке ІМ'Я — the test a callsign, a point or a code has to pass to anchor a line

The transcript is produced and confirmed by human operators, who mishear and mistype like anyone
else, and the speech they are working from is fragmentary. So a name counts only when it is heard
AS a name. This is not an extra prohibition — it is what «названий», «встановлений» and «розібрати»
mean everywhere above.

- **ВЕЛИКІ ЛІТЕРИ = позивний або орієнтир. НІЧОГО ІНШОГО.** Owner's rule, 29.08. Caps in a line
  mean exactly two things: the callsign of a person or unit (`ДЕЦЕЛ`, `ГУСЕЙН`), and the name of a
  point on the ground (`ор ТАЙМЫР`, `т10`, `НЕМО`). A TYPE of thing — a wire obstacle, a vehicle, a
  piece of kit, a code word — is written the way the analyst writes it: lower case, in quotes.
  His own reports are consistent about this: `колючий дріт «Єгоза»`, `«нолик»`, `«воробушек»`,
  `«пончо, халат»`, `«платье»`, `«пятак»` — never caps.

      так:  переміщення в\с ДЕЦЕЛ повз загородження «ежик» (колючий дріт)
      ні:   переміщення в\с ДЕЦЕЛ повз загородження «ЕЖИК»

  This is not cosmetic. On 28.08 the line said `загородження «ЕЖИК»` about a man describing the wire
  beside him («колючка из нержавейки, ежиком называется, вот там я перекурил») — and **ЕЖИК is also
  a real callsign on our own nets** (`переміщення в\с ПРИМОРЕЦ, ЛИС, ЕЖИК о 05:00`). Caps turned a
  type of obstacle into a person who exists, which is the worst kind of error this report can make:
  it reads as fact and it is checkable against nothing.

- **A name is normally a WORD.** Callsigns and point names are ordinary words — animals, objects,
  cities, rivers. A string that is not a word in any language and is not a number is a transcription
  artifact, and it names nothing.

      — Я понял, ему нужно на прось, на прось. Ты же перед просил
      ні:  переміщення в\с АКСАЙ через траншею до ор «ПРОСЬ»

  `прось` is the shorn front of `просил` in the same sentence. Written into a line as an orientir it
  invents a place on the map. When such a token is the only anchor, the movement has no anchor at
  all and there is no line; when a real anchor is also present, the line is written without the
  token.

- **Bare digits are a name only when the exchange leaves no other reading.**

      — Встретил союзника в лесополке.
      — Как позывной? Как позывной у него?
      — Спроси какое подразделение, какое подразделение фамилия, имя, отчество, позывной.
      — 4 72.
      ні:  в\с ВОРОН зустрів союзника, позивний 472

  Four things were asked for in one breath and one fragment came back; it may be the unit, and
  `4 72` may not even be one number. A numeric fragment that could be a unit, a serial or a callsign
  is not a callsign — and since the callsign was the only thing that qualified this exchange under
  §4, the line does not exist.

- Numbers ARE names when the network uses them as such: `т<N>`, `ор` with a number, a code the
  network repeats. The test is usage, not digits.

- The same test applies to a code word before it earns a legend entry: a word nobody can place, that
  occurs once, in one mangled sentence, is not yet a code.

## Merging fragments

Several intercepts often describe ONE event: the start in one, the continuation in another.

- Merge when the link is plain and the line stays a line, not a paragraph.
- The timestamp is the FIRST fragment's.
- **A process is not an event.** If entering a shelter begins at 02:20 and completes at 04:20, the
  event is that they entered.
- Weak link → separate lines. Splitting too much is the safer error.
- The same exchange is sometimes posted twice by different people. Report it once, prefer the fuller
  wording, list both indices in `src`.

## Із чого складається РЯДОК — the three parts, and nothing else

A line is **who**, **what happened**, and **where**. Those three parts are the whole of it. A piece
of the exchange that is none of them has nowhere in the line to go — this is not a prohibition, it
is the shape of the thing.

**"What happened" carries the substance of the event itself** — the nature of a wound, what exactly
was destroyed, that the shelter is being dug. That is the event, and it stays.

What is NOT one of the three parts is commentary AROUND the event: how hard it was, what they are
short of, how much of the way is left, who is annoyed with whom.

    так: 06:01 гр ШПАК залишила т 38, відійшла східніше, обладнує укриття
    ні:  … (лише розпочато, проблеми з доставкою води й лопат)

The fact is that they are digging in at a new place — that is who, what and where, and it is one of
the most valuable things this report carries. Their shortage of water and shovels is neither: the
shelter gets dug with shovels or with hands, and the fact does not change.

    так: 12:03 переміщення в\с ЛЕШИЙ до т8
    ні:  … , залишилось приблизно 600 м

The distance still to walk is a measurement of the same fact, not a fourth part of it. It is true of
every man on the move, it changes every minute, and it says nothing the destination has not said.

## The register

Measured on 1702 real report lines: **median 63 characters**, 79% under 100. Shorter than feels
natural. Telegraphic and nominal, never narrative.

- **Do not open a movement line with `маршрут переміщення`.** The word carries nothing: the reader
  can see it is a route from the orientirs listed in the line, and `переміщення` already covers a
  start, a continuation and an arrival. Write `переміщення в\с ЛИС через лс повз т ТРУБА до позиції
  в\с ЧЕСНОК`. The word itself is not banned — it belongs where a route as such is the subject, e.g.
  `передача деталей маршруту о\с РОВ: т1, т2, т3`.
- No pronouns, no retelling of who said what to whom. State the fact.
- Callsigns and place names in RUSSIAN, uppercase, **undeclined**: `повз укриття в\с СУГРОБ`, never
  `біля Сугроба`.
- Inference is marked `ім` and never dressed as fact.
- Quote a landmark in «» when that is its only identifier.
- Plain `-` as the only dash.
- No explanatory parentheses justifying a reading — that is what `confidence` is for.
- If a line runs past ~90 characters, look for what to drop. Circumstances, reasons and quantities go
  first.

## Whose asset is it — decide by function, not by swearing

They curse their OWN equipment constantly, and an insult attached to a name says nothing about which
side it belongs to. `пидарский Яндекс` means the Яндекс is behaving badly, not that it is ours.
Affiliation is settled by what the thing DOES in the exchange: who launches it, who is asked to send
it, who complains about how it is being used.

    – Это все из этого пидарского Яндекса, я пошел Яндекс искать … меня задела прям в ногу
    – Понял Яндекса, да, Яндекса кидают как попало, не хотят в норку кидать

Here `Яндекс` is THEIR delivery drone: the second speaker agrees it drops badly and misses the
dugout. Reading it as a Ukrainian strike UAV — and writing `300 ім. від БпЛА СОУ «Яндекс»` — inverts
the whole picture. Before attributing a hit to СОУ, check that something in the speech actually
places the thing on our side: the way they talk about it, who controls it, what they expect from it.

When the affiliation cannot be settled, say what happened without it, or mark the reading `ім`.

Everything here is inference. We do not hear every radio, we do not get every word, the transcription
mangles some of what we do get, and they encode the rest on purpose. Write what the speech supports,
mark what it only suggests.
