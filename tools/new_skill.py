#!/usr/bin/env python3
"""
new_skill — scaffold & validate a krevetka skill (SKILL.md).

Part of self-skill-creation (approach B): when a task turns out reusable, distill it
into skills/<name>/SKILL.md. This helper scaffolds the file and validates its shape so
a freshly-written skill is well-formed and immediately discoverable.

Discovery: skills live in skills/<name>/SKILL.md and are exposed to claude-CLI via the
symlink  .claude/skills -> ../skills  (created once; see SELF_SKILL_CREATION_TZ.md).

Usage:
  python3 tools/new_skill.py scaffold <name> ["<one-line description>"]
  python3 tools/new_skill.py validate <name|path/to/SKILL.md>
  python3 tools/new_skill.py list
"""
import os
import re
import sys

BASE = os.environ.get("KREVETKA_BASE",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILLS_DIR = os.path.join(BASE, "skills")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

TEMPLATE = """---
name: {name}
description: {desc}
user-invocable: true
---

{body}
"""


def _skill_path(arg):
    if arg.endswith("SKILL.md"):
        return arg
    return os.path.join(SKILLS_DIR, arg, "SKILL.md")


def parse_frontmatter(text):
    """Return (frontmatter_dict, body_str) or (None, None) if no valid block."""
    if not text.startswith("---"):
        return None, None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None
    fm = {}
    for line in parts[1].splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return fm, parts[2].strip()


def validate(arg):
    path = _skill_path(arg)
    if not os.path.isfile(path):
        print(f"FAIL: no SKILL.md at {path}")
        return 1
    text = open(path, encoding="utf-8").read()
    fm, body = parse_frontmatter(text)
    errs = []
    if fm is None:
        errs.append("missing/invalid YAML frontmatter (--- ... ---)")
    else:
        name = fm.get("name", "")
        if not name:
            errs.append("frontmatter: 'name' missing")
        elif not NAME_RE.match(name):
            errs.append(f"frontmatter: 'name' not kebab-case: {name!r}")
        else:
            dirname = os.path.basename(os.path.dirname(path))
            if dirname != "skills" and dirname != name:
                errs.append(f"'name' ({name}) != dir ({dirname})")
        if not fm.get("description"):
            errs.append("frontmatter: 'description' missing")
        if fm.get("user-invocable", "").lower() not in ("true", "false"):
            errs.append("frontmatter: 'user-invocable' should be true/false")
        if not body or len(body) < 20:
            errs.append("body is empty/too short (needs real instructions)")
    if errs:
        print("INVALID: " + path)
        for e in errs:
            print("  - " + e)
        return 1
    print(f"OK: {path}  (name={fm['name']})")
    return 0


def scaffold(name, desc=None):
    if not NAME_RE.match(name):
        print(f"FAIL: name must be kebab-case (a-z0-9-): {name!r}")
        return 1
    path = _skill_path(name)
    if os.path.exists(path):
        print(f"FAIL: skill already exists: {path}  (edit it, don't overwrite)")
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = TEMPLATE.format(
        name=name,
        desc=desc or "TODO one-line description (specific — used for discovery)",
        body="TODO: imperative instructions the agent follows when this skill runs.",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"scaffolded: {path}")
    print("Next: fill description + body, then `validate`.")
    return 0


def list_skills():
    if not os.path.isdir(SKILLS_DIR):
        print("no skills/ dir")
        return 0
    for name in sorted(os.listdir(SKILLS_DIR)):
        sp = os.path.join(SKILLS_DIR, name, "SKILL.md")
        if os.path.isfile(sp):
            fm, _ = parse_frontmatter(open(sp, encoding="utf-8").read())
            desc = (fm or {}).get("description", "")[:70]
            print(f"  {name:24} {desc}")
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "validate" and len(argv) >= 3:
        return validate(argv[2])
    if cmd == "scaffold" and len(argv) >= 3:
        return scaffold(argv[2], argv[3] if len(argv) >= 4 else None)
    if cmd == "list":
        return list_skills()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
