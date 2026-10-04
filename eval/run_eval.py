#!/usr/bin/env python3
"""Evaluate the meeting-to-action extraction prompt against hand-written ground truth.

Two modes:
  * live  : calls the Claude API with prompts/system_prompt.txt + prompts/tool_schema.json
            (needs ANTHROPIC_API_KEY in the environment)
  * offline: --from-file results.json scores outputs you already have (for example,
            outputs copied from n8n executions). No API key needed.

Metrics per transcript and overall:
  * action recall    : required actions that were found
  * owner accuracy   : among found actions, owner matches (null counts as a real answer)
  * due-date accuracy: among found actions, due_date matches (null when the deadline is vague)
  * hallucinations   : actions that should NOT exist (cancelled or reversed items)
  * decision checks  : final decisions mention what they must, and nothing they must not
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def norm(text):
    """Lowercase, strip accents, and glue digit groups ("340 000" -> "340000")."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"(?<=\d)[\s  ]+(?=\d)", "", text)


def has_all(text, keywords):
    """Every entry in `keywords` must match. An entry is either a word, or a list of
    alternative words (any one of them is enough)."""
    t = norm(text)
    return all(
        any(norm(k) in t for k in (entry if isinstance(entry, list) else [entry]))
        for entry in keywords
    )


def build_user_message(case, transcript):
    d = dt.date.fromisoformat(case["meeting_date"])
    return (
        f"Meeting: {case['meeting_title']}\n"
        f"Date: {case['meeting_date']} ({WEEKDAYS_FR[d.weekday()]})\n"
        f"Participants: {case['participants']}\n\n"
        f"Transcript:\n{transcript}"
    )


def call_claude(case, model):
    import anthropic  # imported here so offline mode works without the package

    system = (ROOT / "prompts" / "system_prompt.txt").read_text(encoding="utf-8").strip()
    tool = json.loads((ROOT / "prompts" / "tool_schema.json").read_text(encoding="utf-8"))
    transcript = (ROOT / case["transcript_file"]).read_text(encoding="utf-8")
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system,
        tools=[tool],
        messages=[{"role": "user", "content": build_user_message(case, transcript)}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Claude did not return a tool_use block")


def as_list(value):
    return value if isinstance(value, list) else [value]


def score_case(case, output):
    actions = output.get("actions", [])
    used = set()
    details = []
    found = owner_ok = due_ok = 0

    for req in case["required_actions"]:
        idx = next(
            (i for i, a in enumerate(actions) if i not in used and has_all(a.get("task", ""), req["keywords"])),
            None,
        )
        if idx is None:
            details.append({"id": req["id"], "found": False})
            continue
        used.add(idx)
        a = actions[idx]
        found += 1
        actual_owner = a.get("owner") or None
        if req["owner"] == "*":
            o_ok = True
        elif req["owner"] is None:
            o_ok = actual_owner is None
        else:
            o_ok = actual_owner is not None and norm(actual_owner) == norm(req["owner"])
        actual_due = a.get("due_date") or None
        d_ok = actual_due in as_list(req["due_date"])
        owner_ok += o_ok
        due_ok += d_ok
        details.append(
            {"id": req["id"], "found": True, "owner_ok": o_ok, "due_ok": d_ok,
             "actual_owner": actual_owner, "actual_due": actual_due}
        )

    halluc = [f["id"] for f in case["forbidden_actions"]
              if any(has_all(a.get("task", ""), f["keywords"]) for a in actions)]

    decisions_text = " | ".join(output.get("decisions", []))
    missing_decision = [k for k in case["decisions_must_contain"] if norm(k) not in norm(decisions_text)]
    bad_decision = [k for k in case["decisions_must_not_contain"] if norm(k) in norm(decisions_text)]

    total = len(case["required_actions"])
    return {
        "id": case["id"],
        "required": total,
        "found": found,
        "owner_ok": owner_ok,
        "due_ok": due_ok,
        "hallucinations": halluc,
        "extra_actions": len(actions) - len(used),
        "decision_problems": missing_decision + bad_decision,
        "details": details,
    }


def pct(a, b):
    return f"{(100 * a / b):.0f}%" if b else "n/a"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--from-file", help="score saved outputs instead of calling the API")
    ap.add_argument("--out", help="write the full results JSON here")
    args = ap.parse_args()

    cases = [json.loads(p.read_text(encoding="utf-8"))
             for p in sorted((ROOT / "eval" / "expected").glob("*.json"))]
    saved = json.loads(Path(args.from_file).read_text(encoding="utf-8")) if args.from_file else None

    outputs, scores = {}, []
    for case in cases:
        if saved is not None:
            output = saved[case["id"]]
        else:
            print(f"Calling {args.model} on {case['id']}...", file=sys.stderr)
            output = call_claude(case, args.model)
        outputs[case["id"]] = output
        scores.append(score_case(case, output))

    print(f"\n{'transcript':32} {'recall':>8} {'owner':>8} {'due date':>9} {'halluc.':>8} {'extras':>7}")
    print("-" * 78)
    for s in scores:
        print(f"{s['id']:32} {pct(s['found'], s['required']):>8} {pct(s['owner_ok'], s['found']):>8} "
              f"{pct(s['due_ok'], s['found']):>9} {len(s['hallucinations']):>8} {s['extra_actions']:>7}")
    T = {k: sum(s[k] for s in scores) for k in ("required", "found", "owner_ok", "due_ok", "extra_actions")}
    H = sum(len(s["hallucinations"]) for s in scores)
    print("-" * 78)
    print(f"{'OVERALL':32} {pct(T['found'], T['required']):>8} {pct(T['owner_ok'], T['found']):>8} "
          f"{pct(T['due_ok'], T['found']):>9} {H:>8} {T['extra_actions']:>7}")

    for s in scores:
        for d in s["details"]:
            if not d["found"]:
                print(f"  [{s['id']}] MISSING action: {d['id']}")
            else:
                if not d["owner_ok"]:
                    print(f"  [{s['id']}] wrong owner for {d['id']}: got {d['actual_owner']!r}")
                if not d["due_ok"]:
                    print(f"  [{s['id']}] wrong due date for {d['id']}: got {d['actual_due']!r}")
        for h in s["hallucinations"]:
            print(f"  [{s['id']}] HALLUCINATED (should not exist): {h}")
        for p in s["decision_problems"]:
            print(f"  [{s['id']}] decision check failed: {p}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"model": args.model if saved is None else "saved-outputs",
                        "date": dt.datetime.now().isoformat(timespec="seconds"),
                        "scores": scores, "outputs": outputs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nFull results written to {args.out}")

    failed = T["found"] < T["required"] or H > 0 or any(s["decision_problems"] for s in scores)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
