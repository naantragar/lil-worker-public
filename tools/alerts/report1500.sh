#!/usr/bin/env bash
# ONE red report a day, instead of the real-time watcher (owner, 15.10.2026).
#
# The watcher (watch.sh fast/slow) was turned off to spare the subscription: it read the air every
# 3 minutes and re-read every hour, ~3200 window-reads a day. This reads each intercept ONCE — a
# single sweep over the 24 h that ends now — and delivers ONLY red, ONLY our direction (Invisible
# Hand + AllInARow, no Patagonia), to the red bot, in the same per-intercept copy-block shape.
#
# Run it from cron at the hour whose PREVIOUS 24 h you want. Cron 15:10 since 16.10.2026: the window
# is ~15:10(yesterday) -> 15:10(today); the owner wants 15:00 -> 15:00 in the end. A full-day sweep
# takes a while, so the messages land ~20-30 min after it starts.
#
#     tools/alerts/report1500.sh            # the real run (cron)
#     DRY=1 tools/alerts/report1500.sh      # read + print, send nothing
set -u
# Cron gives a bare PATH; pin it so the same `claude` is found every time.
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/../.." || exit 1

DOCTRINE="${ALERTS_DOCTRINE:-v7}"
# Opus 5.5 medium since 24.10.2026 (A/B on golden 27.09, knowledge/upstream/alerts/ab_55/): same 12/12
# must-fire reds, same stories, 0 traps at red/yellow for both, 8 false reds vs 13, 2.7x faster,
# ~35% cheaper. Rollback: claude-opus-5 / high.
MODEL="${ALERTS_MODEL:-claude-opus-5-5}"
EFFORT="${ALERTS_EFFORT:-medium}"
HOURS="${ALERTS_HOURS:-24}"
# Bigger windows than the watcher's 10 (owner, 16.10.2026): a daily sweep is not racing the clock,
# so give the model MORE of each net's dialogue per call — a capture or an order reads clearer in 14
# consecutive lines than in 10, and fewer/larger windows is also cheaper. Overlap grows with it so
# the seam between windows still double-covers. Tunable here if a run ever reads worse.
SIZE="${ALERTS_SIZE:-14}"
OVERLAP="${ALERTS_OVERLAP:-3}"
# A state file of its OWN, not the watcher's sent.db: that one is full of everything the watcher
# already marked sent, and reusing it would make the daily report find "0 new" and stay silent.
STATE="knowledge/upstream/alerts/sent_daily.db"
LOG="knowledge/upstream/alerts/report1500.log"
LOCK="/tmp/krevetka-alerts-daily.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') пропущено: попередній звіт ще йде" >> "$LOG"
  exit 0
fi

[ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5000000 ] && { tail -n 2000 "$LOG" > "$LOG.tmp"; mv "$LOG.tmp" "$LOG"; }
echo "$(date '+%F %T') денний звіт: вікно ${HOURS} год, правила ${DOCTRINE}, модель ${MODEL}" >> "$LOG"

RUN=$(python3 tools/alerts/bench.py --hours "$HOURS" --model "$MODEL" --effort "$EFFORT" \
      --doctrine "$DOCTRINE" --workers 4 --tag daily1500 --src ours \
      --size "$SIZE" --overlap "$OVERLAP" 2>>"$LOG" | tail -1)

if [ -z "$RUN" ] || [ ! -f "$RUN" ]; then
  echo "$(date '+%F %T') нічого читати або прогін не дав файла" >> "$LOG"
  exit 0
fi
echo "$(date '+%F %T') прогін: $RUN" >> "$LOG"

# THREE LAYERS, ONE CHRONOLOGICAL DELIVERY (owner, 16.10.2026). Each layer only PRODUCES a file;
# nothing is sent until all three are done, then they are merged, de-duplicated by intercept id and
# sent as ONE batch in publication order. Sending each layer separately sorted each batch on its own,
# so the red bot read out of order.
#   1. the model's reds            ($RUN, level red)
#   2. re-read of the yellows      (recheck_yellow.py: yellow -> red raises, «перепрохід жовтих»)
#   3. model-free safety net       (safety_net.py: capture / coordinates among non-reds, «СТРАХОВКА»)
# "ours" = Invisible Hand + AllInARow: our whole direction (owner, 15.10.2026). Patagonia stays out.
RECHECK="knowledge/upstream/alerts/recheck_daily.json"
NET="knowledge/upstream/alerts/net_daily.json"
COMBINED="knowledge/upstream/alerts/red_daily.json"
rm -f "$RECHECK" "$NET"
python3 tools/alerts/recheck_yellow.py "$RUN" --hours "$HOURS" --out "$RECHECK" >> "$LOG" 2>&1 || \
  echo "$(date '+%F %T') перепрохід жовтих не вдався - йдемо без нього" >> "$LOG"
python3 tools/alerts/safety_net.py "$RUN" --hours "$HOURS" --out "$NET" >> "$LOG" 2>&1 || \
  echo "$(date '+%F %T') страховка не вдалася - йдемо без неї" >> "$LOG"

python3 - "$RUN" "$RECHECK" "$NET" "$COMBINED" >> "$LOG" 2>&1 <<'MERGE'
import json, sys
from pathlib import Path
run, recheck, net, out = sys.argv[1:5]
merged, seen = [], set()
for path, only_red in ((run, True), (recheck, False), (net, False)):
    try:
        alerts = json.loads(Path(path).read_text(encoding="utf-8")).get("alerts", [])
    except Exception:
        continue  # a layer that failed simply contributes nothing
    for a in alerts:
        if only_red and a.get("level") != "red":
            continue
        if str(a.get("id")) in seen:
            continue  # first layer wins: the model's own verdict outranks a re-read or a keyword
        seen.add(str(a.get("id")))
        merged.append(a)
Path(out).write_text(json.dumps({"alerts": merged}, ensure_ascii=False), encoding="utf-8")
print(f"разом до відправки: {len(merged)}")
MERGE

PUSH_ARGS=(--state "$STATE" --level red --source ours --instance alerts_red)
[ "${DRY:-0}" = "1" ] && PUSH_ARGS=(--dry --level red --source ours --instance alerts_red)
python3 tools/alerts/push_tg.py "$COMBINED" "${PUSH_ARGS[@]}" >> "$LOG" 2>&1
echo "$(date '+%F %T') денний звіт завершено" >> "$LOG"
