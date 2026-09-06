# Callsign role inference - build spec

## Why

The register printed under every network header names the men on that net. A role next to a
callsign (`СОСНА - ком склад`) comes from ONE place: the analyst wrote it, once, in one of his own
reports, and we imported it. Our pipeline has never written a role and, as it stands, never will -
`run.py` only SELECTs from `roster`. The file we generate for ourselves says so in as many words:

    без ролі (аналітик не вказав, ми не вигадуємо): ЛЕВША, ЗАЗА

That was the right default while the archive was the only source of truth. It stops being right as
soon as new callsigns start appearing faster than anyone describes them - which is the normal state
of this traffic. **Nobody except us is going to describe them.**

So: a module that reads a callsign's own traffic and proposes what he is. Not integrated into the
report yet - built, measured, and only then argued about.

## The thing that makes this buildable: we have a graded answer key

258 unique callsigns carry a role written by the live analyst. That is a gold set, and it is what
separates this from guessing:

- we can measure accuracy instead of asserting it
- we can tune the abstain threshold against real cost, not intuition
- we can catch the failure that matters most - confident and wrong

Measured 2026-09-04 from `knowledge/upstream/reports.db`:

| | |
|---|---|
| roster rows | 958 |
| rows with a role | 843 |
| unique callsigns | 314 |
| unique callsigns WITH a role | **258** |
| callsigns the analyst described DIFFERENTLY on different days | 33 |

The vocabulary is concentrated, which matters: this is closer to classification into the analyst's
own controlled vocabulary than to free-text generation.

    136  командний склад   (`ком склад`, `мол ком склад`, `кр`, `ком склад шг`, `командування`)
     35  БпЛА              (`БпЛА логістики`, `розрахунок БпЛА`, `БпЛА супроводу`, `ком склад БпЛА`)
     33  накопичувач       (incl. `проміжний накопичувач`, `накопичувач(?)`)
      5  укриття / СП
     49  інше              (the long tail - free descriptions, one-offs)

The 33 callsigns with more than one description are not noise to be cleaned away. `ТИХИЙ` is
`ком склад` on one day and `накопичувач` on another; `ГРОЗНЫЙ` is `командування` and `ст мережі`.
Either the man changed job, or the analyst was describing a different thing about him, or two men
share a callsign on two nets. **The evaluator must treat ANY of his recorded readings as correct.**

## What the module must not become

- **A spammer.** A guess on every silent name is worse than nothing: it fills the register with our
  own noise and, worse, teaches the reader to distrust the entries that came from the analyst.
  Abstaining is a first-class answer and must be the default under thin evidence.
- **A launderer of inference into fact.** Anything we produce is marked, always, and stays
  distinguishable from the analyst's own text forever - in the database, not only in the print.
- **A rewriter.** It never overwrites a role the analyst wrote. Where he has spoken, he is right by
  definition; our reading of the same man is at most a second opinion held separately.

## Shape

Four stages, each runnable and measurable on its own.

### 1. Gold set (`build_gold.py`)

Extract from `reports.db`: callsign -> {every role the analyst ever wrote}, plus which networks and
which report dates. Freeze to JSON. This file is the answer key and must never be produced by the
same code path that produces predictions.

### 2. Evidence (`gather_evidence.py`)

For a callsign on a network: pull his intercepts from the corpus (`messages.db`, read-only, through
the same single door the report uses). What the model gets to see, and nothing else:

- intercepts where he is a station (speaking or being addressed)
- intercepts where he is only mentioned by others - a different and often more telling signal:
  a man who is *reported to* is command, a man who is *asked to deliver* is logistics
- how much air there is: count of intercepts, count of distinct days, total characters

The evidence budget is itself a decision the module makes: below a floor it does not ask the model
at all, it abstains. That floor is a number to be tuned against the gold set, not guessed.

### 3. Inference (`infer_role.py`)

One callsign at a time. Model reads the evidence and returns:

    {"role": "<analyst's vocabulary or free text>", "confidence": 0.0-1.0,
     "basis": "<what in the speech supports it>", "abstain": true|false}

Rules the prompt must carry:

- **Answer in the analyst's own vocabulary where it fits.** `ком склад` and not `командир`,
  `накопичувач` and not `пункт збору`. The controlled list comes from the gold set, so it stays
  his language and not ours.
- Function decides, not tone. The same lesson already written into `rules.md` about equipment:
  who gives orders, who is reported to, who is asked to carry, who is asked to look.
- **Abstain unless the speech shows the function.** Being talkative is not a role.
- `basis` is mandatory and must quote or paraphrase the actual exchange. A verdict with no
  traceable basis is discarded by the harness even if it is right.

### 4. Calibration (`calibrate.py`)

Run stage 3 over the gold callsigns and report, per confidence band:

- **coverage** - share of gold callsigns we answered at all
- **accuracy on answered** - share of answers matching ANY of the analyst's readings for that man
- **the dangerous cell** - high confidence and wrong, listed individually, always, in full

Matching is by class first (`ком склад` vs `мол ком склад` vs `кр` are one class), exact wording
second. Both numbers get reported; the class number is the honest one, the exact-wording number
says how close we are to writing text he would have written himself.

The output of calibration is one number the owner has to approve: **the confidence threshold above
which a proposal is worth showing at all.** Everything below is kept in the working file and never
printed.

## Storage

New table, never `roster`:

    role_guess(callsign, network_id, role, confidence, basis, evidence_n, model, run_at)

Separate table, separate provenance, joined only at print time and only when the threshold is met.
If the module is deleted tomorrow, nothing the analyst wrote is touched.

## Not in scope yet

Integration into the report. That is a separate decision, taken after the calibration numbers
exist and the owner has seen the dangerous cell.

## The card, not the guess - what makes it stable between reports

A verdict produced fresh on every run would flicker: `ком склад` today, `накопичувач` tomorrow,
because each day sees a different slice of the man's traffic. That flicker is worse than silence -
it destroys the reader's trust in the whole register.

So the unit of the module is not a per-report guess, it is a **card that accumulates**:

    role_guess(callsign, network_id, role, confidence, basis, evidence_n, evidence_days,
               first_seen, last_updated, model, status)

    status: proposed | accepted | rejected | superseded

Rules of motion, and they are what give it a memory:

- **Evidence only ever adds.** Each run appends the day's intercepts for that callsign to what is
  already counted. A card at 300 intercepts is not re-derived from today's 12.
- **A verdict is not re-rolled while it holds.** New evidence that AGREES raises confidence. New
  evidence that CONTRADICTS does not silently flip the card - it marks it `розбіжність` and shows
  both readings with both bases. A man who genuinely changed job looks exactly like a wrong first
  guess, and only a human can tell them apart.
- **`rejected` is permanent.** If the owner says a proposal is wrong, that (callsign, role) is never
  proposed again. This is the module's only real learning: it cannot get smarter on its own, but it
  can stop repeating a mistake.
- **`superseded` closes the loop.** The moment the analyst writes his own role for that callsign, our
  card is superseded and stops being printed. Our guess exists only to fill a hole he has not filled.

## How a proposal reaches the page - and why not as `ім`

`ім` and `(?)` are the ANALYST's own marks for his own uncertainty (`накопичувач(?)` stands in 22 of
his rows). Printing our guess with his marks would make the two indistinguishable within a week, and
the whole discipline of the register is that his text is his.

So a proposal never goes inline into his register. It goes into a **separate, fenced block** under
it, plainly labelled as ours, e.g.:

    наші припущення (не від аналітика):
      КАЩЕЙ - ім ст мережі, приймає доповіді про повітря від ПУХ, ЧЕСНОК, МАВР

That block is also the delivery mechanism: the chief analyst reads it, and if he agrees he writes it
into HIS next report - at which point the import turns it into gold, the card goes `superseded`, and
our line disappears. The module's success is measured by how many of its proposals stop being its
own.

## Four gates against over-eagerness

Measured on the real corpus, 22.08-05.09 (20 202 intercepts):

- **313** callsigns known, **257** already described by the analyst - untouchable
- **56** without a role - the entire candidate pool
- **24** of those have >=3 intercepts in 14 days
- **8** have serious material (>=98 intercepts): КАЩЕЙ 313, МЕДВЕДЬ 305, ЛЮТЫЙ 160, ХОНДА 122,
  УРАЛ 115, ОРЕЛ 106, БЕРЕГ 98, СПАРТАК 67

So the honest ceiling is not "a guess on every name in every report" - it is **at most a couple of
dozen cards in total**, most of them written once and then left alone. The gates:

1. **Evidence floor** - below the threshold the model is not asked at all. Not a low-confidence
   answer: no call, no cost, no line.
2. **Confidence threshold** - the number the owner approves after calibration. Below it the card
   lives in the working file and is never printed.
3. **Quota per report** - a hard cap on printed proposals. If a run wants to propose more than the
   cap, it prints the strongest and logs the rest. A page that suddenly sprouts fifteen guesses is a
   bug however good each one is.
4. **Never over the analyst** - a card for a callsign he has described is dead on arrival.

## Noticed while counting, unrelated but real

`СПАРТАК` and `СПАРТАК/ПАРТАК` come back as two separate candidate rows with identical evidence
(67 intercepts each). `fold_variants()` merges them inside the register, but the gold set builder
keeps them apart. Same man, counted twice. Fix in `build_gold.py`, not in the register.

# PART II - the dossier (2026-09-05, after four refuted hypotheses)

## What the role hunt actually established

Four attempts to read a role out of a 14-day snapshot of raw speech, each measured against the
analyst's own answer key:

| hypothesis | measured | verdict |
|---|---|---|
| the instrument misleads: a man who flies a drone is often command | error `ком склад -> БпЛА` 16 -> 6 after one prompt rule | **CONFIRMED, shipped** |
| an accumulator is a place, and a place does not speak | 80% "himself" both classes | refuted |
| an accumulator only receives, a commander gives orders | orders/100 lines: accumulator 9.7, commander 4.0 | refuted, and INVERTED |
| an accumulator is named as a destination in speech | 0.0% vs 0.0% | no signal at all |

Only the first axis is learnable from a snapshot. `ком склад` vs `накопичувач` is not: the analyst
draws it from where the man physically sits, which is not in the traffic. Same category as
`БЕЛЫЙ - водій АТ` (151 intercepts, not one word about a vehicle).

Also fixed in the same pass, all of it in the RULER rather than the model: `ком склад БпЛА` is
command over drones and not a drone crew (priority in `CLASSES`), `км склад` / `командир` /
`кр 2мср` / `ком розрахунку` were falling into `інше` and scoring correct answers as misses, and
the analyst's free notes (`регулярне прибуття на позиції СМИРНИЙ`, bare numbers) are not roles and
are no longer graded against. 51.8% -> 56.1% without a single new model call.

## The turn: accumulate relations, do not guess labels

The owner's idea, and it is the right one. Instead of one shot at a label, fold the report's
`_events.json` day by day into a per-callsign card of OBSERVED RELATIONS. The events are the point:
the relations there are already extracted and normalised by the report pass.

Measured on 29 days: "named as a destination" separates the classes **11.1% vs 2.4%** in events,
where the same feature over raw speech gave **0.0% vs 0.0%**. The distillation IS the signal.

`dossier.py` stores no verdicts. Per callsign: who ordered whom, who was ordered by whom, who was a
destination, who appeared with whom, on how many DISTINCT DAYS, on which nets, with a sample line
and its `_src_ref` for every claim. Three invariants:

- **strength is counted in distinct days and distinct counterparts**, never in repetitions of one
  line - one loud night cannot make a fact
- **every observation keeps its source** and can be walked back to the intercept
- **our own inference never reinforces itself** - these are relations lifted from lines, so a wrong
  line yields a wrong OBSERVATION, traceable and correctable, not a belief that grows with repeats

## Why the air counter is not optional

The owner asked the sharp question: if a man never earns an event, is he not by definition
unimportant to us? Measured, and the answer is no - and the bias runs exactly the wrong way.

    events per 1000 intercepts
      накопичувач      218     an accumulator is WHERE people go, so he is written into the line
      командний склад   34     a commander is the man TALKING ABOUT somebody else's move
      БпЛА              28

Of the twelve loudest callsigns with zero events, **ten are command staff in the analyst's own
hand** - ГРАНИТ 52/0, БЕРКУТ 50/0, ХОРОШИЙ 48/0, two company commanders among them. A filter on
event count discards precisely the people the register exists for.

His instinct was right but about a DIFFERENT level of selection: the rear-echelon officer nobody
needs is filtered by NET and BAND - a header naming no formation never enters the report at all -
not by whether a man earned a line. Two filters, not interchangeable.

So every callsign the analyst ever wrote gets a card, seeded from the answer key, even with zero
events. The card then says honestly `подій: ЖОДНОЇ, ЕФІР: 52 перехоплення за 12 діб`. Currently
22 such cards - the run prints their count on every fold.

## Next

- daily fold after each report (seconds, no model)
- threshold on INDEPENDENT observations - three distinct days and three distinct counterparts before
  a relation counts as established - not on a model's confidence
- only then, on accumulated relations, try the role hypotheses again

## PLACE OR PERSON - the discriminator four measurements missed

Found 2026-09-05 while reading the first list of established relations: half of them were fruit.
ИЗЮМ, БЕРГАМОТ, ХУРМА, БРУСНИКА, АРОНИЯ, ПОГРЕБ - and ЗАЛІЗНИЧНЕ, a settlement.

The signal is the RATIO of events to air, and that is why it was missed: every earlier attempt
looked at ONE number. A place is written into report lines constantly, because people are sent to
it, and is silent on the radio, because it has no radio. A man is the other way round.

    ЗАЛІЗНИЧНЕ  events/air 26.0   air 0
    ПОГРЕБ                  5.25  air 4
    ХУРМА                   4.75  air 4
    ИЗЮМ                    3.88  air 16
    ------------------------------------- 1.0
    ПУХ                     0.41  air 59
    БУРЫЙ                   0.35  air 97    in the analyst's register
    ДОН                     0.34  air 121   in the analyst's register

An order of magnitude, clean, and both names the analyst himself registered land on the human side.
Implemented as an ADVISORY flag, never a filter - it is printed on the card and such a card is not
offered as a person until somebody looks. 68 of 1210 cards fire it.

Caveat kept in view: ДОН is both. He is a callsign in the register AND a treeline others walk to
(`до лс ДОН`, `ВУ по ор ДОН`). The ratio puts him on the human side correctly, but every event
about him is about the place. One word can be two things and the dossier must not pretend
otherwise.

## Threshold and daily fold - implemented

    MIN_DAYS = 3, MIN_PARTNERS = 2

A relation is ESTABLISHED only after that many DISTINCT days and DISTINCT counterparts. On the
threshold of independent observations, never on a model's confidence - the whole point is that
nothing here is asserted, only counted.

Honest first result on 29 days of events: **14 people established, 246 accumulating, 68 flagged as
landmarks.** Even ГУСЕЙН - 1200 intercepts, 106 events - does NOT clear it, because his relations
are one day per partner. That is the threshold working, not failing.

`daily.sh` folds after each report. It REBUILDS from every events file rather than appending, on
purpose: the fold is cheap and idempotent, so re-running it after a report has been regenerated
(as 05.09 was) picks up the corrected lines instead of carrying the old observation forever. Both
it and `dossier.py` are registered in `tools/hooks/durable_commands.json` - the air counter reads
the whole corpus and takes minutes.
