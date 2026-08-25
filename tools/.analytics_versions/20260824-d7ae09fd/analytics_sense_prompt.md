# Pass C: make sense of a whole window at once

You are given a compressed table for one reporting window: every callsign that was on the air, how
often it spoke, how often it was mentioned, in which networks and on which frequencies, who its
regular partners were, and a few lines of its speech.

You also receive the day's extracted event lines.

This is the layer that the per-thread pass cannot see. A single exchange never reveals a callsign's
function; twenty do. Your job is to read the aggregate.

## Answer these, and only from the evidence given

1. **Ком склад.** Who gives orders, receives reports, allocates people and assets? A commander
   typically appears across several networks and frequencies, talks to many partners, and speaks in
   imperatives. Distinguish levels where possible: ст ком склад, ком склад, мол ком склад, кр, кв.
2. **БпЛА.** Who commands a drone group, who is a pilot, who is a crew. Who reports drone activity
   rather than performing it.
3. **Накопичувачі та проміжні укриття.** Which positions do several groups converge on, get led
   through, or wait at? Name the position and who runs it.
4. **Штурмові групи.** Which callsigns form one, who leads it, where it is being led.
5. **«Союзники».** Which callsigns belong to an adjacent unit rather than the speakers' own, and
   which unit.
6. **Code words.** Words used in an unusual, non-literal sense — a plain noun standing for a
   direction, an action, an object. In the raw air they carry NO quotes and NO marking: `«кубок»`
   with quotes is the analyst's notation, the speakers just say "идём на кубок". Look for a word
   that recurs in positions where a place, a direction or an order belongs. Give the reading and the
   evidence.
   **Decode everything you can, including words the desk already knows** — a known word still helps
   you read the traffic around it. A separate list of already-known readings is filtered out
   downstream, so nothing is lost by including one and nothing is gained by withholding it. What the
   report is actually FOR is the word that is NOT yet in anybody's legend: a new brevity code, a new
   name for a position, a new signal.
7. **Same callsign, different people.** A name appearing in unrelated networks is as likely to be a
   coincidence as a commander working several nets. Say which, and why.
8. **One group on several frequencies.** Frequencies that carry the same callsigns and the same
   activity are probably one network. This is a hypothesis, never a fact.

## Output — JSON only

    {
      "callsign_roles": [
        {"name":"ДЕД","net":3,"role":"ім ком склад рівня мсб",
         "confidence":0.7,"evidence":"6 мереж, 5 частот, наказовий тон, партнери з різних груп"}
      ],
      "positions": [
        {"name":"…","kind":"накопичувач|проміжне укриття|позиція","who":"…","confidence":0.6,
         "evidence":"…"}
      ],
      "codes": [
        {"word":"кубок","net":5,"reading":"напрямок руху північ",
         "confidence":0.5,"evidence":"тричі в конструкції «йти на …», один раз «за … поверни»"}
      ],
      "groups": [
        {"members":["…"],"kind":"шг|розрахунок БпЛА|союзники","leader":"…","confidence":0.6,
         "evidence":"…"}
      ],
      "network_links": [
        {"freqs":["…","…"],"claim":"ім одна мережа","confidence":0.5,"evidence":"…"}
      ]
    }

## The register of a roster line

This layer is roughly HALF of the finished report, and in it a role is written in a fixed, very
terse vocabulary. Measured on real reports:

    БОРЕЦ- ком склад
    МОТОМОТА- кр 4мср 2мсб
    АВГУСТ- мол ком склад, проміжний накопичувач. Старший позиції ШАХМАТИСТ
    СКИФ- розрахунок БпЛА доставка МТЗ, розвідка
    ЛИЗГИН- ком склад БпЛА
    ЗАЯЦ- проміжне укриття 1198 мсп
    ГРОМ – БпЛА логістики, DJI Mavic
    КУЗЯ – СП
    МАЛИНА – розрахунок БпЛА перехоплювача
    ВОЛГА – ком склад шг, ім кв.

The vocabulary is closed — use these terms, not synonyms: `ст ком склад`, `ком склад`,
`мол ком склад`, `кр`, `кв`, `ком склад шг`, `ком склад БпЛА`, `розрахунок БпЛА`, `БпЛА логістики`,
`БпЛА супроводу`, `БпЛА перехоплювач`, `накопичувач`, `проміжний накопичувач`, `проміжне укриття`,
`СП`, `транспортування шг`, `логістика`, `розвідка`. Add the unit (`4 мср 2 мсб`, `1198 мсп`) when
the traffic gives it.

**Rank-and-file do not belong in the roster.** A callsign that is simply a soldier being moved,
reporting in, or being talked about is not roster material, however often it was on the air — it is
correct analysis and useless in the report. List a callsign ONLY if you can attribute a FUNCTION to
it: any level of ком склад, кр, кв, a drone crew or pilot, a СП, whoever holds a накопичувач or a
проміжне укриття, logistics, транспортування. **A row with no role is dropped before printing**, so
emitting one is wasted work — either establish the function or leave the callsign out.

**A function you suspect but cannot confirm IS worth emitting** — give it the role and a low
`confidence`, and it will be printed marked, exactly as the analyst does it:

    МАРК – накопичувач(?)
    ЕЖИК – накопичувач(?)

That mark is a request to the supervisor to re-check, and a suspected накопичувач flagged for
re-check is far more useful than a callsign silently left out. Withhold only when you have no reading
at all, not when the reading is merely uncertain.

Legend lines are just as terse, and the reading is the meaning in use:

    «55» - прийнято, зрозуміло
    «нолик» - БпЛА, йм DJI Mavic
    «82» - продовження руху
    «зебра» - ім ротація
    «пушистики» - о\с ім буд яких підрозділів «союзники»

## Naming the network — use the number, never the words

Every network in the material is headed `## [N] <шапка>`. In `net` put that **integer N** and nothing
else. Do not retype the header, do not invent a descriptor like `«кілька мереж (416.19, 147.50)»` —
a roster line whose network cannot be resolved is silently dropped from the report, and that is half
the product lost. If a callsign genuinely works several networks, emit one row per network.

## Discipline

- **Everything here is a hypothesis.** The enemy does not announce its structure; all of this is
  reconstructed from listening. Never write a claim as established fact, and set `confidence`
  honestly — 0.9 means the evidence is nearly explicit, 0.4 means it is a reading worth checking.
- `evidence` is mandatory and must point at what is actually in the table: counts, networks, the
  wording of a line. "Схоже на командира" without a reason is worthless.
- Prefer FEWER, better-supported claims. An empty list is better than a list of guesses.
- Names stay in Russian, uppercase, undeclined — exactly as they appear.
- Reply in Ukrainian inside the fields. JSON only, no prose around it.
