"""Replay the pre-post review over saved eval runs, without touching the lab.

For every saved run this takes the tool calls and results the agent already gathered and its
draft write-up, runs the review step on them, and regrades the outcome. Same evidence, one
variable: it shows how many wrong answers the review fixes and how many right ones it breaks.

    python replay_review.py --infile results.jsonl --mode fresh --out results.review-fresh.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path.home() / "agent"))

import run_evals as ev          # noqa: E402  (also loads the agent's .env)
import runner                   # noqa: E402

RECORD_SYSTEM = "You restate incident write-ups as JSON."


def rebuild_messages(row: dict, transcript: list) -> list:
    """The investigator's conversation, rebuilt from the saved transcript (for --mode same)."""
    alert = {"status": "firing", "labels": row.get("labels", {}), "startsAt": row.get("alert_started")}
    msgs = [{"role": "system", "content": runner.SYSTEM},
            {"role": "user", "content": f"Time now: {row.get('ts')}\nAlert payload from Grafana:\n" + json.dumps(alert)}]
    for i, t in enumerate(transcript):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": t["tool"], "arguments": json.dumps(t["args"])}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": t["result"]})
    msgs.append({"role": "assistant", "content": row["text"]})
    return msgs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default="results.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["fresh", "same"], default="fresh")
    ap.add_argument("--configs", default="")
    args = ap.parse_args()
    scenarios = {s["id"]: s for s in yaml.safe_load((HERE / "scenarios.yaml").read_text())}
    writeups = {}
    for line in (ev.AGENT / "writeups.jsonl").read_text().splitlines():
        try:
            w = json.loads(line)
            writeups[w.get("run_id")] = w
        except json.JSONDecodeError:
            pass
    want = {c.strip() for c in args.configs.split(",") if c.strip()}
    out = (HERE / args.out).open("a")
    for line in (HERE / args.infile).read_text().splitlines():
        row = json.loads(line)
        if row.get("status") != "ok" or (want and row["config"] not in want):
            continue
        scn = scenarios[row["scenario"]]
        transcript = json.loads((ev.AGENT / "transcripts" / f"{row['run_id']}.json").read_text())
        w = writeups.get(row["run_id"], {})
        full = {**w, **row}
        usage = {"llm_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        alert_text = json.dumps({"alertname": row["alert"], "labels": w.get("labels", {}), "startsAt": w.get("alert_started")})
        msgs = runner.review_messages(args.mode, rebuild_messages(full, transcript), alert_text, transcript, row["text"])
        final, review = runner.run_review(row["model"], msgs, row["text"], usage)
        changed = final != row["text"]
        record = row["record"]
        if changed:
            resp = runner.client.chat.completions.create(
                model=row["model"], response_format={"type": "json_object"},
                messages=[{"role": "system", "content": RECORD_SYSTEM},
                          {"role": "user", "content": "WRITE-UP:\n" + final + "\n\n" + runner.RECORD_PROMPT}])
            record = json.loads(resp.choices[0].message.content or "{}")
        # Regrade. The judge is only asked again when the text changed, so an unchanged
        # answer keeps its original cause/fix grade and judge noise stays out of the comparison.
        graded = ev.grade(scn, {"run_id": row["run_id"], "text": final, "record": record}, row["t_truth"]) if changed else None
        quotes = ev.check_quotes(final, transcript)
        checks = dict(graded["checks"]) if changed else dict(row["checks"])
        checks["quotes"] = quotes["checked"] > 0 and not quotes["missing"]
        result = {
            "run_id": row["run_id"], "scenario": row["scenario"], "config": row["config"], "repeat": row["repeat"],
            "mode": args.mode, "verdict": review.get("verdict"), "changed": changed,
            "before": row["checks"], "after": checks, "all_pass_after": all(v is True for v in checks.values()),
            "review": review, "review_cost_usd": runner.cost_usd(usage, row["model"]), "review_usage": usage,
            "record": record, "text": final, "draft_text": row["text"] if changed else None,
            "judge_notes": graded["judge_notes"] if changed else row.get("judge_notes"),
            "quotes_missing": quotes["missing"],
        }
        out.write(json.dumps(result) + "\n")
        out.flush()
        print(row["config"], row["scenario"], f"r{row['repeat']}", review.get("verdict"),
              "cause", row["checks"]["cause"], "->", checks["cause"], f"${result['review_cost_usd']}", flush=True)


if __name__ == "__main__":
    main()
