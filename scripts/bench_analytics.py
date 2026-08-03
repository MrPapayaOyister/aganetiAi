"""Benchmark the analytics agent's retrieval behaviour. Read-only: it only asks questions.

Measures, per question:
  round_trips  how many tool calls the agent made (the efficiency contract's main target)
  wall_ms      end-to-end latency
  ttft_ms      time to the first token
  verdict      the Stage-1 critic status
  numbers      the figures stated in the answer, so correctness can be diffed run-to-run

Usage:
    python bench_sql.py before.json          # record a baseline
    python bench_sql.py after.json before.json   # record and diff against a baseline
"""
import json
import os
import re
import subprocess
import sys
import time

BASE = "http://localhost:8000"
ENV = os.path.expanduser("~/aganetiAi/.env")
TOK = ""
for line in open(ENV):
    if line.startswith("INTERNAL_API_TOKEN="):
        TOK = line.split("=", 1)[1].strip()
        break

# Representative of what a Dar Al Ber user actually asks. Deliberately spans the shapes
# the efficiency contract cares about: single aggregate, grouped breakdown, top-N (the
# row-goal trap), a time series, a ratio, and a multi-part question that invites the
# model to issue several round-trips where one would do.
QUESTIONS = [
    ("total", "What is the total approved expenditure?"),
    ("breakdown", "Break down total approved expenditure by category."),
    ("topn", "What are the top 5 categories by approved expenditure?"),
    ("byemirate", "Break down approved expenditure by emirate."),
    ("trend", "Show approved expenditure by month over the last 12 months."),
    ("ratio", "What is the average approved grant size?"),
    ("count", "How many aid requests were approved?"),
    ("multipart", "Break down approved expenditure by category, and also give me the "
                  "overall total and the average grant size."),
]

NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?!\d)(?!,\d)")


def ask(question):
    t0 = time.time()
    cmd = ["curl", "-s", "-N", "-X", "POST", BASE + "/dashboard/ask",
           "-H", "Content-Type: application/json",
           "-H", "X-Internal-Token: " + TOK,
           "-H", "X-Internal-User: user_1",
           "-d", json.dumps({"message": question}), "--max-time", "300"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    ttft = None
    tool_calls, tool_results, verdict, final = [], [], None, ""
    for ln in proc.stdout:
        if not ln.startswith("data:"):
            continue
        body = ln[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            e = json.loads(body)
        except Exception:
            continue
        t = e.get("type")
        if t == "token" and ttft is None:
            ttft = (time.time() - t0) * 1000
        elif t == "tool_call":
            tool_calls.append({k: e.get(k) for k in ("name", "sql", "args", "stage") if e.get(k)})
        elif t == "tool_result":
            tool_results.append({k: e.get(k) for k in ("name", "ok", "rows", "ms") if k in e})
        elif t == "stage" and e.get("stage") == "critic":
            verdict = (e.get("detail") or {}).get("status")
        elif t == "done":
            final = e.get("final") or ""
    proc.wait()
    wall = (time.time() - t0) * 1000

    nums = []
    for m in NUM.finditer(final):
        raw = m.group(1)
        v = float(raw.replace(",", ""))
        if abs(v) >= 1000 and v not in nums:
            nums.append(v)

    return {
        "question": question,
        "round_trips": len(tool_calls),
        "wall_ms": round(wall),
        "ttft_ms": round(ttft) if ttft else None,
        "verdict": verdict,
        "answer_chars": len(final),
        "numbers": sorted(nums, reverse=True)[:12],
        "tool_calls": tool_calls,
        "tool_results": tool_results,
    }


out_path = sys.argv[1] if len(sys.argv) > 1 else "bench.json"
baseline_path = sys.argv[2] if len(sys.argv) > 2 else None

results = {}
print("%-11s %6s %8s %8s %-11s %s" % ("case", "calls", "wall_ms", "ttft_ms", "verdict", "top figure"))
print("-" * 78)
for key, q in QUESTIONS:
    try:
        r = ask(q)
    except Exception as exc:  # noqa: BLE001
        r = {"question": q, "error": str(exc), "round_trips": None, "wall_ms": None}
        print("%-11s  ERROR %s" % (key, exc))
        results[key] = r
        continue
    results[key] = r
    top = "{:,.0f}".format(r["numbers"][0]) if r["numbers"] else "-"
    print("%-11s %6s %8s %8s %-11s %s"
          % (key, r["round_trips"], r["wall_ms"], r["ttft_ms"], r["verdict"] or "-", top))

json.dump(results, open(out_path, "w"), indent=1)
print("\nwrote " + out_path)

tot_calls = sum(r.get("round_trips") or 0 for r in results.values())
tot_wall = sum(r.get("wall_ms") or 0 for r in results.values())
print("TOTAL round-trips: %d   TOTAL wall: %.1fs   mean: %.1fs"
      % (tot_calls, tot_wall / 1000, tot_wall / 1000 / max(1, len(results))))

if baseline_path and os.path.exists(baseline_path):
    base = json.load(open(baseline_path))
    print("\n%-11s %-19s %-19s %s" % ("case", "round-trips", "wall_ms", "figures"))
    print("-" * 78)
    for key in results:
        b, a = base.get(key, {}), results[key]
        same = "same" if b.get("numbers") == a.get("numbers") else "CHANGED"
        print("%-11s %-19s %-19s %s"
              % (key,
                 "%s -> %s" % (b.get("round_trips"), a.get("round_trips")),
                 "%s -> %s" % (b.get("wall_ms"), a.get("wall_ms")),
                 same))
        if same == "CHANGED":
            print("             was: %s" % (b.get("numbers") or [])[:5])
            print("             now: %s" % (a.get("numbers") or [])[:5])
