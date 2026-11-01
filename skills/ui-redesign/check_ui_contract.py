"""
UI contract checker for the Weltrade bot redesign.

Usage (run from the project root):
    python skills/ui-redesign/check_ui_contract.py snapshot   # once, BEFORE any edits
    python skills/ui-redesign/check_ui_contract.py compare    # after each phase

It records what the scripts in static/index.html depend on (element ids and their
tag types, inline handlers, input names, function names, API endpoints) and fails
if any of it goes missing or changes. New additions are allowed and only listed.
"""
import json
import pathlib
import re
import sys

HTML = pathlib.Path("static/index.html")
BASELINE = pathlib.Path("ui_contract_baseline.json")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def extract(text: str) -> dict:
    # id -> tag, includes ids built inside JS template strings (they contain ${...})
    id_tags = {}
    for tag, rest in re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^<>]*?)>", text, flags=re.S):
        m = re.search(r'\bid="([^"]+)"', rest)
        if m:
            id_tags.setdefault(m.group(1), set()).add(tag.lower())

    handlers = set()
    for tag, rest in re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^<>]*?)>", text, flags=re.S):
        for attr, val in re.findall(r'\b(on(?:click|change|input|submit|toggle))="([^"]*)"', rest):
            id_m = re.search(r'\bid="([^"]+)"', rest)
            where = id_m.group(1) if id_m else tag.lower()
            handlers.add(f"{where} :: {attr}={norm(val)}")

    names = sorted(set(re.findall(r'\bname="([^"]+)"', text)))
    functions = sorted(set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", text)))
    endpoints = sorted(set(re.findall(r"apiCall\(\s*[`'\"]([^`'\"?]+)", text)))
    lookups = sorted(set(re.findall(r"getElementById\(\s*[`'\"]([^`'\"]+)[`'\"]", text)))

    return {
        "ids": {k: sorted(v) for k, v in sorted(id_tags.items())},
        "handlers": sorted(handlers),
        "names": names,
        "functions": functions,
        "endpoints": endpoints,
        "lookups": lookups,
    }


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"snapshot", "compare"}:
        print(__doc__)
        return 2
    if not HTML.exists():
        print(f"Cannot find {HTML}. Run from the project root.")
        return 2

    current = extract(HTML.read_text(encoding="utf-8"))

    if sys.argv[1] == "snapshot":
        BASELINE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        print(f"Baseline saved to {BASELINE}: {len(current['ids'])} ids, "
              f"{len(current['handlers'])} handlers, {len(current['functions'])} functions.")
        return 0

    if not BASELINE.exists():
        print("No baseline found. Run `snapshot` before editing.")
        return 2

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    problems = []

    for key in ("names", "functions", "endpoints", "lookups", "handlers"):
        missing = sorted(set(base[key]) - set(current[key]))
        added = sorted(set(current[key]) - set(base[key]))
        for item in missing:
            problems.append(f"MISSING {key[:-1] if key.endswith('s') else key}: {item}")
        for item in added:
            print(f"note: new {key} entry: {item}")

    for id_, tags in base["ids"].items():
        if id_ not in current["ids"]:
            problems.append(f"MISSING id: {id_}")
        elif current["ids"][id_] != tags:
            problems.append(f"CHANGED tag for id {id_}: {tags} -> {current['ids'][id_]}")
    for id_ in current["ids"]:
        if id_ not in base["ids"]:
            print(f"note: new id: {id_}")

    if problems:
        print("\nUI contract broken:")
        for p in problems:
            print("  " + p)
        return 1

    print("UI contract intact: every id, handler, name, function and endpoint is still present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())