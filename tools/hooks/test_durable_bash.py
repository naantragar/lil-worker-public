"""Does the durable-bash hook still convert the real long runners, and stop converting mentions?"""
import sys
sys.path.insert(0, "~/lil_worker/tools/hooks")
import durable_bash as H

MUST_CONVERT = [
    "python3 tools/analytics2/run.py --day 2026-08-30",
    "timeout 60 python3 tools/analytics2/run.py --day 2026-08-30",
    "cd ~/lil_worker && python3 tools/analytics2/run.py --day 2026-08-30",
    "python3 tools/analytics2/run.py --from '2026-08-29 15:00' --to '2026-08-30 15:00' --out X",
    "python3 ~/lil_worker/tools/analytics_run.py --from A --to B",
    "bash tools/analytics_pipeline.sh 2026-08-30",
    "./tools/analytics_pipeline.sh",
    "python3 tools/analytics_sense.py --from A --to B",
    "python3 tools/analytics2/movement_count.py --freq 445.5000 --days 2026-08-28",
    "nohup python3 tools/analytics2/run.py --day 2026-08-30",
]

MUST_NOT_CONVERT = [
    # the incident that started this
    "ls tools/analytics_pipeline.sh",
    "ls -la tools/analytics2/run.py",
    "cat tools/analytics_pipeline.sh",
    "head -20 tools/analytics2/run.py",
    "grep -n 'run.py --day' tools/analytics2/run.py",
    "grep -rn 'analytics_run.py --from' tools/",
    "git diff tools/analytics2/run.py",
    "git log -1 -- tools/analytics_pipeline.sh",
    "wc -l tools/analytics2/run.py",
    "echo 'python3 tools/analytics2/run.py --day 2026-08-30'",
    "stat tools/analytics2/movement_count.py --freq",   # nonsense, but must not convert
    # fast mode of the very same script
    "python3 tools/analytics2/run.py --day 2026-08-29 --render-only",
    "timeout 300 python3 tools/analytics2/run.py --day 2026-08-29 --render-only --out /tmp/x",
    # unrelated commands
    "python3 tools/memory_search.py search 'upstream'",
    "bot/run.sh restart",
]

# Services, not tasks. These must stay inline even when asked for in the background: a durable job
# that never finishes holds its room's gate forever and can only be killed by hand. See _never().
MUST_BE_SERVICE = [
    "cd ~/upstream-system/web && npm run dev -- --port 5178 --strictPort",
    "npm run dev",
    "pnpm dev",
    "yarn start",
    "npx vite --port 5178",
    "next dev -p 3000",
    "nodemon server.js",
    "uvicorn api.app.main:app --reload",
    "python3 -m http.server 8080",
    "tail -f bot/lil_worker.log",
]
# ...while the FINITE sibling of a service must not be caught by an over-broad never-pattern.
NOT_A_SERVICE = [
    "npm run build",
    "npm ci",
    "vite build",
    "python3 tools/analytics2/run.py --day 2026-09-16",
]

fails = []
print("=== ДОЛЖНЫ конвертироваться ===")
for c in MUST_CONVERT:
    got = H._match(c)
    ok = got is not None
    print(f"  {'OK ' if ok else 'FAIL'}  {c[:78]}")
    if not ok:
        fails.append(("должна конвертироваться", c))

print("\n=== НЕ должны конвертироваться ===")
for c in MUST_NOT_CONVERT:
    got = H._match(c)
    ok = got is None
    print(f"  {'OK ' if ok else 'FAIL'}  {c[:78]}")
    if not ok:
        fails.append(("ложное срабатывание", c))

print("\n=== СЛУЖБЫ: никогда не в джобу, даже в фоне ===")
for c in MUST_BE_SERVICE:
    ok = H._never(c) is not None
    print(f"  {'OK ' if ok else 'FAIL'}  {c[:78]}")
    if not ok:
        fails.append(("служба ушла бы в джобу", c))

print("\n=== ...а конечный сосед службы — не служба ===")
for c in NOT_A_SERVICE:
    ok = H._never(c) is None
    print(f"  {'OK ' if ok else 'FAIL'}  {c[:78]}")
    if not ok:
        fails.append(("never-шаблон слишком широкий", c))

total = len(MUST_CONVERT) + len(MUST_NOT_CONVERT) + len(MUST_BE_SERVICE) + len(NOT_A_SERVICE)
print()
print(f"ВСЕ {total} СЛУЧАЕВ ПРОШЛИ" if not fails else f"ПРОВАЛОВ: {len(fails)}")
for why, c in fails:
    print(f"   {why}: {c}")
sys.exit(1 if fails else 0)
