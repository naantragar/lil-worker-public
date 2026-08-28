---
name: dash-to-hyphen
description: Replace typographic dashes (em/en dash, minus sign, unicode hyphen) with the plain ASCII hyphen "-" across files, adding spaces only where a dash was glued between words. Use when asked to "replace dashes with hyphens", "no em dashes", "убери длинные тире", or before shipping text a user types with plain hyphens only.
user-invocable: true
---

# Dash to hyphen

Typographic dashes are a giveaway of generated text and they are not what the user types.
Convert them to the plain ASCII hyphen-minus `-`, without inventing compound words.

## When to run

- The user says any of: "replace the dashes with hyphens", "no long dashes", "убери тире",
  "замени тире на дефисы".
- A file, page, bot, menu or document is about to ship and its text must match a user who
  types only `-`.
- Ask nothing else. The request is the whole spec - the rules below are the agreed behaviour.

## The one rule that makes this non-trivial

A plain `str.replace` is wrong in both directions:

- `word—word` -> `word-word` invents a compound word that never existed. It must become
  `word - word`, so decide spacing **from the context first**, then emit the hyphen.
- A real hyphen inside `well-known` must NOT gain spaces. Never touch existing `-`.

So the order is: find each dash, look at the characters on both sides, add the spaces that
are missing, and only then write the hyphen.

## How to run

```
python3 skills/dash-to-hyphen/scripts/dash_to_hyphen.py <file-or-dir> [...]        # dry run
python3 skills/dash-to-hyphen/scripts/dash_to_hyphen.py <file-or-dir> --write      # apply
python3 skills/dash-to-hyphen/scripts/dash_to_hyphen.py --self-test                # 15 cases
```

- Dry run is the default and prints a `- old` / `+ new` line for every change. **Read it before
  writing** - that diff is the review step.
- Directories are walked recursively, `.git` skipped, extensions filtered (`--ext`, default
  covers py/md/txt/js/ts/html/css/json/yml/sh/sql).
- Binary and non-UTF-8 files are skipped silently.

## What it does, exactly

| Input | Output | Why |
|---|---|---|
| `word—word` | `word - word` | glued dash would fake a compound word |
| `word — word` | `word - word` | spacing already fine, only the codepoint changes |
| `word— word`, `word —word` | `word - word` | one side missing a space |
| `2020–2024` | `2020-2024` | a numeric range stays tight |
| `— Dialogue` | `- Dialogue` | line-start marker, no leading space added |
| `well-known` | unchanged | a real hyphen is never touched |
| `e‑mail` (U+2011) | `e-mail` | already a hyphen, only the codepoint is wrong |
| `−5` (U+2212) | `-5` | already a minus |
| `---` | unchanged | ASCII already, incl. markdown rules |

Covered dashes: U+2014 em, U+2013 en, U+2012 figure, U+2015 horizontal bar, U+2E3A/U+2E3B
two- and three-em. Covered look-alikes: U+2010, U+2011, U+2212.

## After running

1. Re-run the tool's dry run (or `git diff`) and confirm nothing but dashes moved.
2. If the target is running code, restart the service so the new text is live, and
   re-run whatever check that project uses (syntax/validate) first.
3. Tell the user in one line which files changed.

## Editing this skill

Every dash in `scripts/dash_to_hyphen.py` is written as a `\uXXXX` escape **on purpose**: with
literal characters, running the tool over its own folder would rewrite its own tables and break
it. Keep it that way, and keep `--self-test` green after any change.
