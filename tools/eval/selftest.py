#!/usr/bin/env python3
"""Offline unit tests for the eval CHECK ENGINE.

Verifies contains / regex / equals / script check functions against CANNED
outputs WITHOUT calling the model. Fast (seconds), zero token cost. This is the
cheap correctness gate for the scoring logic.

Run either:
    python3 tools/eval/selftest.py
    python3 tools/eval/run.py --selftest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run  # noqa: E402  (the harness module under test)


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def expect(passed, score, cond_passed, cond_score=None):
    """Assert a (passed, score, detail) result. cond_score optional exact match."""
    ok = passed == cond_passed
    if cond_score is not None:
        ok = ok and abs(score - cond_score) < 1e-9
    return ok


# ---- contains -------------------------------------------------------------

@case("contains: present -> pass, score 1")
def _():
    p, s, _d = run.check_contains("hello # Example Domain world",
                                  {"type": "contains", "value": "# Example Domain"})
    return expect(p, s, True, 1.0)


@case("contains: absent -> fail, score 0")
def _():
    p, s, _d = run.check_contains("nothing here", {"type": "contains", "value": "MISSING"})
    return expect(p, s, False, 0.0)


@case("contains: ignore_case works")
def _():
    p, s, _d = run.check_contains("HELLO WORLD",
                                  {"type": "contains", "value": "hello", "ignore_case": True})
    return expect(p, s, True, 1.0)


# ---- regex ----------------------------------------------------------------

@case("regex: match -> pass")
def _():
    p, s, _d = run.check_regex("order id: 4211 done",
                               {"type": "regex", "value": r"id:\s*\d+"})
    return expect(p, s, True, 1.0)


@case("regex: no match -> fail")
def _():
    p, s, _d = run.check_regex("no digits", {"type": "regex", "value": r"\d{4}"})
    return expect(p, s, False, 0.0)


@case("regex: bad pattern -> fail, not crash")
def _():
    p, s, _d = run.check_regex("x", {"type": "regex", "value": "("})
    return expect(p, s, False, 0.0)


# ---- equals ---------------------------------------------------------------

@case("equals: stripped match -> pass")
def _():
    p, s, _d = run.check_equals("  yes\n", {"type": "equals", "value": "yes"})
    return expect(p, s, True, 1.0)


@case("equals: mismatch -> fail")
def _():
    p, s, _d = run.check_equals("yes", {"type": "equals", "value": "no"})
    return expect(p, s, False, 0.0)


@case("equals: strict keeps whitespace -> fail")
def _():
    p, s, _d = run.check_equals(" yes ", {"type": "equals", "value": "yes", "strict": True})
    return expect(p, s, False, 0.0)


# ---- script ---------------------------------------------------------------

@case("script: exit 0 -> pass")
def _():
    p, s, _d = run.check_script("", {"type": "script", "command": "true"})
    return expect(p, s, True, 1.0)


@case("script: nonzero exit -> fail")
def _():
    p, s, _d = run.check_script("", {"type": "script", "command": "false"})
    return expect(p, s, False, 0.0)


@case("script: expect_contains matches stdout")
def _():
    p, s, _d = run.check_script("", {"type": "script",
                                     "command": "printf 'PONG'",
                                     "expect_contains": "PONG"})
    return expect(p, s, True, 1.0)


@case("script: expect_contains missing -> fail")
def _():
    p, s, _d = run.check_script("", {"type": "script",
                                     "command": "printf 'PONG'",
                                     "expect_contains": "NOPE"})
    return expect(p, s, False, 0.0)


@case("script: pass_output feeds stdin")
def _():
    p, s, _d = run.check_script("markdown body",
                                {"type": "script", "command": "cat",
                                 "expect_contains": "markdown", "pass_output": True})
    return expect(p, s, True, 1.0)


@case("script: missing command -> fail")
def _():
    p, s, _d = run.check_script("", {"type": "script"})
    return expect(p, s, False, 0.0)


# ---- dispatch / misc ------------------------------------------------------

@case("run_check: unknown type -> fail")
def _():
    p, s, _d = run.run_check("x", {"type": "bogus"})
    return expect(p, s, False, 0.0)


@case("parse_score: extracts decimal in range")
def _():
    return run.parse_score("0.75") == 0.75 and run.parse_score("The score is 1.0") == 1.0


@case("parse_score: out-of-range / garbage -> None")
def _():
    return run.parse_score("nonsense") is None and run.parse_score("7") is None


@case("parse_score: trailing sentence period tolerated")
def _():
    return (run.parse_score("0.9.") == 0.9
            and run.parse_score("Rating: 0.85.") == 0.85
            and run.parse_score("The score is 0.8.") == 0.8)


@case("parse_score: comma decimal normalized, negatives rejected")
def _():
    return run.parse_score("0,8") == 0.8 and run.parse_score("-0.5") is None


@case("contains: empty/missing value -> fail (not silent pass)")
def _():
    p1, s1, _d = run.check_contains("anything", {"type": "contains", "value": ""})
    p2, s2, _e = run.check_contains("anything", {"type": "contains"})
    return expect(p1, s1, False, 0.0) and expect(p2, s2, False, 0.0)


@case("script: timeout -> fail (not crash)")
def _():
    p, s, d = run.check_script("", {"type": "script", "command": "sleep 5", "timeout": 0.2})
    return expect(p, s, False, 0.0) and "timed out" in d


@case("run_judge: aggregates mean/variance/threshold (mocked model)")
def _():
    canned = iter(["0.9", "0.8", "1.0"])
    orig = run.call_claude
    run.call_claude = lambda prompt, timeout=None: (True, next(canned), "")
    try:
        passed, mean, detail = run.run_judge("ans", {"value": "exp", "threshold": 0.7}, runs=3)
    finally:
        run.call_claude = orig
    return passed is True and abs(mean - 0.9) < 1e-9 and "var=" in detail


@case("tiny_yaml_parse: flat + nested check block")
def _():
    text = ("id: demo\n"
            "skill: markdown-new\n"
            "input: \"hello\"\n"
            "check:\n"
            "  type: contains\n"
            "  value: \"# Example\"\n"
            "  ignore_case: true\n")
    d = run._tiny_yaml_parse(text)
    return (d.get("id") == "demo" and d.get("skill") == "markdown-new"
            and d.get("check", {}).get("type") == "contains"
            and d.get("check", {}).get("value") == "# Example"
            and d.get("check", {}).get("ignore_case") is True)


@case("aggregate: computes score + pass_rate")
def _():
    agg = run.aggregate([
        {"passed": True, "score": 1.0},
        {"passed": False, "score": 0.0},
    ])
    return agg["cases"] == 2 and agg["passed"] == 1 and agg["aggregate_score"] == 0.5


def main():
    failures = []
    for name, fn in CASES:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            ok = False
            name = f"{name}  [EXC {exc!r}]"
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {name}")
        if not ok:
            failures.append(name)
    total = len(CASES)
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
