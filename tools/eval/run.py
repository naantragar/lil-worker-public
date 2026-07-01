#!/usr/bin/env python3
"""Offline eval harness (v0).

Runs a set of cases through the `claude -p` CLI (the same binary the bot uses)
and scores each against an expectation. Produces a readable table, one aggregate
score, and a JSON result file under tools/eval/results/.

Design goals:
- The CHECK ENGINE is pure and separately testable (see --selftest / selftest.py).
  Scoring correctness is verifiable in seconds with zero token cost.
- OFFLINE: no network beyond the model call itself.
- Every subprocess call has a timeout (per project CLAUDE.md rules).
- Generic & secret-free: repo root is derived relative to this file, never hardcoded.

Check types:
- contains : output must contain a substring
- regex    : output must match a regex (re.search)
- equals   : output, stripped, must equal a value
- script   : run a shell command; check exit code and/or stdout substring
- judge    : a separate `claude -p` call scores 0-1 vs a fixed rubric (N runs, mean+variance)

Usage:
    python3 tools/eval/run.py                 # run all cases
    python3 tools/eval/run.py --only <id>     # single case
    python3 tools/eval/run.py --skill <name>  # cases tagged for one skill
    python3 tools/eval/run.py --list          # list discovered cases, no model calls
    python3 tools/eval/run.py --compare A B   # before/after: two git refs -> score diff
    python3 tools/eval/run.py --selftest      # unit-test the check engine on canned data
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- repo root, derived relative to this file (no hardcoded personal paths) ---
EVAL_DIR = Path(__file__).resolve().parent
CASES_DIR = EVAL_DIR / "cases"
RESULTS_DIR = EVAL_DIR / "results"
REPO_ROOT = EVAL_DIR.parent.parent  # tools/eval/ -> tools/ -> repo root

# Default timeouts (seconds). Kept modest per CLAUDE.md timeout rule.
MODEL_TIMEOUT = 180
SCRIPT_TIMEOUT = 60
JUDGE_TIMEOUT = 120
JUDGE_RUNS = 3

# Fixed, versioned judge rubric. Keep generic & secret-free.
JUDGE_RUBRIC_VERSION = "v0"
JUDGE_PROMPT_TEMPLATE = """You are a strict evaluation judge. Score how well the ANSWER
satisfies the EXPECTATION, on a scale from 0.0 (completely fails) to 1.0 (fully satisfies).

Rubric:
- 1.0  fully and correctly satisfies the expectation
- 0.5  partially satisfies it (relevant but incomplete or partly wrong)
- 0.0  does not satisfy it at all (irrelevant, empty, or wrong)

Respond with ONLY a single decimal number between 0 and 1. No words, no explanation.

EXPECTATION:
{expectation}

ANSWER:
{answer}
"""


# ============================================================================
# CHECK ENGINE (pure — no model calls, no I/O beyond `script` subprocess).
# These functions are unit-tested by selftest against canned outputs.
# Each returns (passed: bool, score: float, detail: str).
# ============================================================================

def check_contains(output, check):
    """Pass if `output` contains check['value'] (optionally case-insensitive)."""
    value = str(check.get("value", ""))
    if not value:
        return False, 0.0, "contains check missing non-empty value"
    text = output
    if check.get("ignore_case"):
        value = value.lower()
        text = text.lower()
    ok = value in text
    return ok, (1.0 if ok else 0.0), ("found" if ok else f"missing substring: {value!r}")


def check_regex(output, check):
    """Pass if regex check['value'] matches anywhere in `output` (re.search)."""
    pattern = str(check.get("value", ""))
    flags = re.IGNORECASE if check.get("ignore_case") else 0
    try:
        matched = re.search(pattern, output, flags) is not None
    except re.error as exc:
        return False, 0.0, f"bad regex: {exc}"
    return matched, (1.0 if matched else 0.0), ("matched" if matched else f"no match for /{pattern}/")


def check_equals(output, check):
    """Pass if output.strip() == value (value also stripped unless strict)."""
    value = str(check.get("value", ""))
    got = output if check.get("strict") else output.strip()
    expected = value if check.get("strict") else value.strip()
    ok = got == expected
    return ok, (1.0 if ok else 0.0), ("equal" if ok else "not equal")


def check_script(output, check):
    """Run a shell command, assert on exit code and/or stdout substring.

    check:
      command: shell command to run (required)
      expect_exit: expected exit code (default 0)
      expect_contains: optional substring that must appear in the command's stdout
      pass_output: if true, `output` is fed to the command's stdin
      timeout: optional override (seconds)
    The command runs with cwd = repo root so relative paths are stable.
    """
    command = check.get("command")
    if not command:
        return False, 0.0, "script check missing 'command'"
    expect_exit = int(check.get("expect_exit", 0))
    timeout = float(check.get("timeout", SCRIPT_TIMEOUT))
    stdin_data = output if check.get("pass_output") else None
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(REPO_ROOT),
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, 0.0, f"script timed out after {timeout}s"
    if proc.returncode != expect_exit:
        return False, 0.0, f"exit {proc.returncode} != expected {expect_exit}"
    want = check.get("expect_contains")
    if want is not None and str(want) not in proc.stdout:
        return False, 0.0, f"stdout missing {want!r}"
    return True, 1.0, "script ok"


# Registry of PURE / deterministic checks (safe to unit-test without a model).
PURE_CHECKS = {
    "contains": check_contains,
    "regex": check_regex,
    "equals": check_equals,
    "script": check_script,
}


def run_check(output, check):
    """Dispatch a check. `judge` is handled separately (needs model). Returns
    (passed, score, detail). Unknown types are treated as errors (fail)."""
    ctype = check.get("type")
    fn = PURE_CHECKS.get(ctype)
    if fn is None:
        return False, 0.0, f"unknown check type: {ctype!r}"
    return fn(output, check)


# ============================================================================
# MODEL CALLS (claude -p) — only reached during real runs, never in selftest.
# ============================================================================

def call_claude(prompt, timeout=MODEL_TIMEOUT):
    """Call `claude -p <prompt>` and return (ok, stdout, err_detail)."""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "", "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "", f"claude timed out after {timeout}s"
    if proc.returncode != 0:
        return False, proc.stdout, f"claude exit {proc.returncode}: {proc.stderr.strip()[:200]}"
    return True, proc.stdout, ""


def parse_score(text):
    """Extract the first 0..1 decimal from judge output; None if unparseable.

    Tolerant of a trailing period (a judge ending a sentence: '0.9.', 'Rating: 0.85.'),
    normalizes a single comma decimal ('0,8' -> '0.8'), and rejects negatives (a '-'
    immediately before the number blocks the match rather than silently dropping the sign)."""
    s = text.strip()
    # normalize a comma decimal like "0,8" -> "0.8" (comma flanked by digits)
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    # a number not preceded by a digit/dot/minus, and not immediately followed by a
    # digit (a following '.' IS allowed — trailing sentence period).
    m = re.search(r"(?<![\d.\-])(\d(?:\.\d+)?)(?!\d)", s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if 0.0 <= val <= 1.0:
        return val
    return None


def run_judge(output, check, runs=JUDGE_RUNS):
    """LLM-judge: N model calls scoring 0-1 vs the fixed rubric. Returns
    (passed, mean_score, detail). Variance is reported in detail."""
    expectation = str(check.get("value") or check.get("rubric") or "")
    prompt = JUDGE_PROMPT_TEMPLATE.format(expectation=expectation, answer=output)
    threshold = float(check.get("threshold", 0.7))
    scores = []
    for _ in range(runs):
        ok, out, err = call_claude(prompt, timeout=JUDGE_TIMEOUT)
        if not ok:
            return False, 0.0, f"judge call failed: {err}"
        val = parse_score(out)
        if val is None:
            return False, 0.0, f"judge unparseable: {out.strip()[:80]!r}"
        scores.append(val)
    mean = sum(scores) / len(scores)
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    passed = mean >= threshold
    detail = f"mean={mean:.2f} var={var:.3f} n={runs} thr={threshold} scores={scores}"
    return passed, mean, detail


# ============================================================================
# CASE LOADING
# ============================================================================

def _load_yaml_or_json(path):
    """Load a case file. Prefer PyYAML; fall back to json for .json; else a
    tiny flat-yaml hand-parser (enough for the simple case schema used here)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml  # noqa: WPS433
        return yaml.safe_load(text)
    except ImportError:
        return _tiny_yaml_parse(text)


def _tiny_yaml_parse(text):
    """Minimal YAML subset parser: top-level key: value, plus a single nested
    `check:` block (2-space indented key: value). Handles quoted strings and
    ints/bools. This is a FALLBACK only (PyYAML preferred)."""
    result = {}
    current_nested = None
    nested_key = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        coerced = _coerce_scalar(val) if val else None
        if indent == 0:
            if coerced is None:
                # start of a nested block (e.g. "check:")
                current_nested = {}
                nested_key = key
                result[key] = current_nested
            else:
                result[key] = coerced
                current_nested = None
                nested_key = None
        else:
            if current_nested is None:
                current_nested = {}
                result[nested_key or "check"] = current_nested
            current_nested[key] = coerced
    return result


def _coerce_scalar(val):
    if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
        return val[1:-1]
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "~"):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def load_cases():
    """Load all cases from cases/*.yaml|*.yml|*.json, sorted by id."""
    cases = []
    for path in sorted(CASES_DIR.glob("*")):
        if path.suffix not in (".yaml", ".yml", ".json"):
            continue
        data = _load_yaml_or_json(path)
        if not isinstance(data, dict):
            continue
        data.setdefault("id", path.stem)
        data["_file"] = str(path.relative_to(REPO_ROOT))
        cases.append(data)
    cases.sort(key=lambda c: c.get("id", ""))
    return cases


# ============================================================================
# RUNNING
# ============================================================================

def run_one_case(case):
    """Execute a single case end-to-end. Returns a result dict."""
    check = case.get("check", {}) or {}
    ctype = check.get("type")
    started = time.time()

    # Obtain the model output. For a `script` check with no input, the check
    # can run standalone (output ignored); but we still call the model if an
    # `input` is present so behavior is exercised.
    model_output = ""
    model_err = ""
    model_called = False
    if case.get("input") is not None:
        model_called = True
        ok, model_output, model_err = call_claude(str(case["input"]))
        if not ok:
            return {
                "id": case.get("id"),
                "skill": case.get("skill"),
                "type": ctype,
                "passed": False,
                "score": 0.0,
                "detail": f"model call failed: {model_err}",
                "duration_s": round(time.time() - started, 2),
            }

    if ctype == "judge":
        passed, score, detail = run_judge(model_output, check)
    else:
        passed, score, detail = run_check(model_output, check)

    return {
        "id": case.get("id"),
        "skill": case.get("skill"),
        "type": ctype,
        "passed": passed,
        "score": round(score, 3),
        "detail": detail,
        "model_called": model_called,
        "duration_s": round(time.time() - started, 2),
    }


def filter_cases(cases, only=None, skill=None):
    out = cases
    if only:
        out = [c for c in out if c.get("id") == only]
    if skill:
        out = [c for c in out if c.get("skill") == skill]
    return out


def print_table(results):
    if not results:
        print("(no cases matched)")
        return
    id_w = max(len(str(r["id"])) for r in results)
    id_w = max(id_w, 4)
    print(f"{'PASS':<5} {'ID':<{id_w}} {'TYPE':<9} {'SCORE':<6} DETAIL")
    print("-" * (5 + id_w + 9 + 6 + 20))
    for r in results:
        mark = "ok " if r["passed"] else "FAIL"
        print(f"{mark:<5} {str(r['id']):<{id_w}} {str(r['type']):<9} "
              f"{r['score']:<6} {r['detail'][:70]}")


def aggregate(results):
    if not results:
        return {"cases": 0, "passed": 0, "aggregate_score": 0.0, "pass_rate": 0.0}
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    score = sum(r["score"] for r in results) / total
    return {
        "cases": total,
        "passed": passed,
        "aggregate_score": round(score, 3),
        "pass_rate": round(passed / total, 3),
    }


def write_results(results, agg, extra=None):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "timestamp": ts,
        "judge_rubric_version": JUDGE_RUBRIC_VERSION,
        "aggregate": agg,
        "results": results,
    }
    if extra:
        payload.update(extra)
    out_path = RESULTS_DIR / f"{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def run_suite(cases):
    results = [run_one_case(c) for c in cases]
    print_table(results)
    agg = aggregate(results)
    print()
    print(f"AGGREGATE  score={agg['aggregate_score']}  "
          f"pass={agg['passed']}/{agg['cases']}  rate={agg['pass_rate']}")
    return results, agg


# ============================================================================
# COMPARE MODE (before/after via two git refs)
# ============================================================================

def _git(args):
    return subprocess.run(["git"] + args, cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=30)


def _current_ref():
    r = _git(["symbolic-ref", "--quiet", "--short", "HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    r = _git(["rev-parse", "HEAD"])
    return r.stdout.strip()


def run_compare(ref_before, ref_after, only=None, skill=None):
    """Checkout ref_before, run suite; checkout ref_after, run suite; diff.
    Requires a clean working tree. Restores original ref at the end. Honors the
    same --only/--skill filters so both refs run the identical scoped subset."""
    status = _git(["status", "--porcelain"])
    if status.stdout.strip():
        print("ERROR: working tree not clean; commit/stash before --compare.")
        return 2
    original = _current_ref()
    scores = {}
    try:
        for label, ref in (("before", ref_before), ("after", ref_after)):
            co = _git(["checkout", ref])
            if co.returncode != 0:
                print(f"ERROR: checkout {ref} failed: {co.stderr.strip()}")
                return 2
            print(f"\n=== {label}: {ref} ===")
            cases = filter_cases(load_cases(), only=only, skill=skill)
            _, agg = run_suite(cases)
            scores[label] = agg
    finally:
        _git(["checkout", original])
    before = scores.get("before", {}).get("aggregate_score", 0.0)
    after = scores.get("after", {}).get("aggregate_score", 0.0)
    delta = round(after - before, 3)
    print(f"\n=== COMPARE ===")
    print(f"before={before}  after={after}  delta={delta:+}")
    write_results([], {}, extra={"compare": {
        "before_ref": ref_before, "after_ref": ref_after,
        "before": scores.get("before"), "after": scores.get("after"),
        "delta": delta,
    }})
    return 0


# ============================================================================
# SELFTEST (delegates to selftest.py; no model calls)
# ============================================================================

def run_selftest():
    import selftest  # local module in same dir
    return selftest.main()


# ============================================================================
# CLI
# ============================================================================

def build_parser():
    p = argparse.ArgumentParser(description="Offline eval harness (v0)")
    p.add_argument("--only", help="run a single case by id")
    p.add_argument("--skill", help="run only cases tagged for this skill")
    p.add_argument("--list", action="store_true", help="list cases, no model calls")
    p.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                   help="run suite on two git refs and diff aggregate scores")
    p.add_argument("--selftest", action="store_true",
                   help="unit-test the check engine on canned data (no model calls)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.selftest:
        sys.path.insert(0, str(EVAL_DIR))
        return run_selftest()

    if args.compare:
        return run_compare(args.compare[0], args.compare[1],
                           only=args.only, skill=args.skill)

    cases = load_cases()
    cases = filter_cases(cases, only=args.only, skill=args.skill)

    if args.list:
        for c in cases:
            skill = c.get("skill") or "-"
            ctype = (c.get("check") or {}).get("type") or "?"
            print(f"{str(c.get('id')):<28} skill={skill:<14} "
                  f"type={ctype:<9} {c.get('_file')}")
        print(f"\n{len(cases)} case(s).")
        return 0

    if not cases:
        # Exit 2 (not 1) so scripts can tell an empty/typo'd selection apart
        # from a genuine test FAIL (which returns 1 below).
        print("No cases matched. Use --list to see available cases.")
        return 2

    results, agg = run_suite(cases)
    out_path = write_results(results, agg)
    print(f"\nWrote {out_path.relative_to(REPO_ROOT)}")
    return 0 if agg["passed"] == agg["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
