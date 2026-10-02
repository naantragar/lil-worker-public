#!/usr/bin/env bash
# The alerting watcher. Two lanes over the same brain, the same doctrine and the same state file.
#
#     tools/alerts/watch.sh fast      # every 3 min: only windows with something unread
#     tools/alerts/watch.sh slow      # hourly: the whole 3 hours again, thread now complete
#     tools/alerts/watch.sh once 240  # one-off sweep over an arbitrary window
#
# WHY TWO LANES. Speed and correctness pull opposite ways. A three-minute-old intercept is often
# unreadable on its own — «взяли в плен» means nothing without the next three transmissions — so a
# fast lane alone would misjudge fresh threads. A slow lane alone costs the owner twenty minutes of
# latency on the one message that matters. So: the fast lane reports what is already legible, the
# slow lane re-reads the same traffic once the thread has finished arriving and can UPGRADE a line
# that first looked like nothing. An upgrade delivers by itself — `log` is never written to sent.db,
# so the same intercept raised to `push` later is, correctly, new.
#
# WHY IT IS NOT MORE WORK THAN THE OLD 20-MINUTE CADENCE. Measured 29.09.2026: a 45-min slice is
# ~24 model-windows, but three minutes of traffic touches only ~4 nets, so only ~4 windows contain
# anything unread. 480 fast steps x ~4 + 24 slow steps x 46 ~= 3200 window-reads a day, against
# 3312 for the old 72 steps x 46. Same load, latency down from ~14 min to ~4.
set -u
cd "$(dirname "$0")/../.." || exit 1

LANE="${1:-fast}"
DOCTRINE="${ALERTS_DOCTRINE:-v7}"
MODEL="${ALERTS_MODEL:-claude-opus-5}"
EFFORT="${ALERTS_EFFORT:-high}"
STATE="knowledge/upstream/alerts/sent.db"
LOG="knowledge/upstream/alerts/watch.log"
LOCK="/tmp/krevetka-alerts-watch.lock"

case "$LANE" in
  fast) WINDOW=45;  ONLY_NEW=1 ;;
  slow) WINDOW=180; ONLY_NEW=0 ;;
  once) WINDOW="${2:-180}"; ONLY_NEW=0 ;;
  *) echo "смуга: fast | slow | once [хвилин]"; exit 2 ;;
esac

# One lock for BOTH lanes: they write the same state file, and a slow step overlapping a fast one
# would race on `seen`. A fast tick that lands during a slow sweep is simply dropped — the next one
# is three minutes away, and the slow sweep is reading that traffic anyway.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') [$LANE] пропущено: попередній крок ще йде" >> "$LOG"
  exit 0
fi

[ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5000000 ] && { tail -n 2000 "$LOG" > "$LOG.tmp"; mv "$LOG.tmp" "$LOG"; }

stamp() { date '+%F %T'; }
echo "$(stamp) [$LANE] вікно ${WINDOW} хв, правила ${DOCTRINE}" >> "$LOG"

ARGS=(--minutes "$WINDOW" --model "$MODEL" --effort "$EFFORT" --doctrine "$DOCTRINE"
      --workers 4 --tag "$LANE")
[ "$ONLY_NEW" = "1" ] && ARGS+=(--only-new "$STATE")

RUN=$(python3 tools/alerts/bench.py "${ARGS[@]}" 2>>"$LOG" | tail -1)

if [ -z "$RUN" ] || [ ! -f "$RUN" ]; then
  echo "$(stamp) [$LANE] нічого читати або прогін не дав файла" >> "$LOG"
  exit 0
fi

# ДВА КАНАЛИ З ОДНОГО ПРОХОДУ. Модель уже поділила: red - те, де названо МІСЦЕ або НАСЛІДОК,
# yellow - решта по темі. Другого читання це не коштує, ділиться готовий результат.
# Червоний іде першим: якщо щось піде не так із жовтим, найважче вже доставлено.
python3 tools/alerts/push_tg.py "$RUN" --state "$STATE" --level red \
        --instance alerts_red >> "$LOG" 2>&1
python3 tools/alerts/push_tg.py "$RUN" --state "$STATE" --level yellow \
        --instance helper >> "$LOG" 2>&1
echo "$(stamp) [$LANE] завершено" >> "$LOG"
