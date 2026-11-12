"""Eval harness. Runs on the lab host, next to the alert runner.

For each (configuration, scenario, repeat):
  reset the lab to a clean, healthy state -> wait for a baseline -> plant the failure
  -> wait for the agent's write-up -> grade it -> append one line to results.jsonl

Usage:
  ~/agent/.venv/bin/python run_evals.py                         # everything, once
  ~/agent/.venv/bin/python run_evals.py --only api_stopped_by_hand --configs gpt-5.4
  ~/agent/.venv/bin/python run_evals.py --repeats 3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).parent
AGENT = Path.home() / "agent"
LAB = Path.home() / "lab"
RESULTS = HERE / "results.jsonl"
GOOD_COMMIT = "8d3fb2b"
JUDGE_MODEL = "gpt-5.5"
BASELINE_SECONDS = 150          # healthy data before the failure, so trends have a "before"
WRITEUP_TIMEOUT = 660
START_TOLERANCE = 120           # seconds either side of the true start

CONFIGS = {
    "gpt-5.4": {"model": "gpt-5.4", "disabled_tools": []},
    "gpt-5.4-mini": {"model": "gpt-5.4-mini", "disabled_tools": []},
    # Same model without the two workflow tools: raw metrics and logs only.
    "gpt-5.4 primitives only": {"model": "gpt-5.4", "disabled_tools": ["what_changed", "log_summary"]},
}

for line in (AGENT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def sh(cmd: str, timeout: int = 600) -> str:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "") + (p.stderr or "")


def say(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def prom(query: str) -> list:
    try:
        r = httpx.get("http://127.0.0.1:9090/api/v1/query", params={"query": query}, timeout=10)
        return r.json()["data"]["result"]
    except Exception:
        return []


def alerts_inactive() -> bool:
    try:
        r = httpx.get("http://127.0.0.1:3000/api/prometheus/grafana/api/v1/rules",
                      auth=("admin", os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin")), timeout=10)
        states = re.findall(r'"state":"([a-z]+)"', r.text)
        return bool(states) and all(s == "inactive" for s in states)
    except Exception:
        return False


def running_version() -> str | None:
    env = sh("sudo docker inspect lab-orders-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}'")
    m = re.search(r"APP_VERSION=(\S+)", env)
    return m.group(1) if m else None


def cleanup(scn: dict | None) -> None:
    """Undo a planted failure. Rolls the app back only if a scenario deployed something."""
    if scn:
        sh(scn.get("cleanup", "true"))
    sh("curl -s -X POST http://127.0.0.1:8080/admin/debug-capture/off")
    sh("sudo docker exec lab-backup-1 rm -f /tmp/break_verify")
    sh("sudo rm -f /mnt/mongo-data/export_tmp.bin")
    if running_version() != GOOD_COMMIT:
        say("  rollback deploy: " + sh(f"bash {LAB}/scripts/jenkins-deploy.sh last-good").strip()[-60:])
    sh("sudo docker start lab-orders-api-1")


def reset() -> None:
    """A clean, healthy lab with no leftover metrics or logs from earlier runs."""
    cleanup(None)
    sh(f"cd {LAB} && sudo docker compose stop mongodb orders-api loadgen backup prometheus loki")
    sh("sudo find /mnt/mongo-data -mindepth 1 -delete")
    # Wipe metrics and logs so one run's incident cannot leak into the next one's evidence.
    sh(f"cd {LAB} && sudo docker compose rm -f prometheus loki")
    sh("sudo docker volume rm lab_prometheus-data lab_loki-data")
    sh(f"cd {LAB} && sudo docker compose up -d")
    sh("sudo systemctl restart alert-runner")
    deadline = time.time() + 300
    while time.time() < deadline:
        up = prom("up")
        traffic = sh("curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health").strip()
        if len(up) >= 3 and all(s["value"][1] == "1" for s in up) and alerts_inactive() and traffic == "200":
            return
        time.sleep(5)
    raise RuntimeError("lab did not return to healthy within 5 minutes")


def wait_writeup(run_id: str) -> dict | None:
    path = AGENT / "writeups.jsonl"
    deadline = time.time() + WRITEUP_TIMEOUT
    while time.time() < deadline:
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("run_id") == run_id:
                    return row
        time.sleep(5)
    return None


# ------------------------------------------------------------------ grading

def parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _norm(text: str) -> str:
    return re.sub(r'[\s\\"\'`]', "", text.lower())


def check_quotes(text: str, transcript: list) -> dict:
    """Evidence the write-up puts in `code spans` should appear in what the tools returned.

    Checked: spans of 12+ characters that contain a word. Skipped: bare numbers and times
    (the model reformats them) and metric queries the model wrote itself.
    """
    hay = _norm(json.dumps(transcript))
    checked, missing = 0, []
    for span in dict.fromkeys(re.findall(r"`([^`\n]+)`", text)):
        if len(span) < 12 or not re.search(r"[A-Za-z]{4}", span):
            continue
        if "{" in span and "=" in span and "}" in span and '":' not in span:
            continue            # a PromQL selector the model composed, not quoted evidence
        parts = [p for p in re.split(r"\.\.\.|…", span) if len(_norm(p)) >= 8]
        if not parts:
            continue
        checked += 1
        if not all(_norm(p) in hay for p in parts):
            missing.append(span[:120])
    return {"checked": checked, "missing": missing}


JUDGE_PROMPT = """You are grading an incident write-up produced by an automated investigator against an answer key.
Be strict and literal. Grade only against the key; do not reward plausible-sounding guesses.

Return one JSON object with exactly these keys:
"cause_correct": true or false,
"fix_acceptable": true or false,
"wrongly_blamed": a short string naming anything the write-up blamed that the key says must not be blamed, or "",
"notes": one or two sentences explaining the grades.

Rules for cause_correct:
- true only if the write-up's stated root cause matches the true cause and covers every item in "required".
- false if it asserts, as the cause, anything listed in "must_not_blame".
- If the key says the cause is not determinable, the write-up is correct only if it clearly says the
  cause could not be determined (while reporting what it could establish) and does not present a guess as fact.
- Mentioning a must_not_blame item only to rule it out is fine.

Rules for fix_acceptable:
- true if following the proposed fix would resolve the true problem and matches the spirit of the key's fix,
  without a step that would make things worse. Extra sensible steps are fine.
- false if the fix targets the wrong cause or would not resolve the problem."""


def judge(scn: dict, text: str, record: dict | None) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    key = {k: scn["truth"][k] for k in ("cause", "required", "must_not_blame", "fix", "cause_determinable")}
    resp = client.chat.completions.create(
        model=JUDGE_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": "ANSWER KEY:\n" + json.dumps(key, indent=1)
             + "\n\nWRITE-UP:\n" + text + "\n\nSTRUCTURED RECORD:\n" + json.dumps(record)},
        ],
    )
    return json.loads(resp.choices[0].message.content)


def grade(scn: dict, row: dict, t_truth: float) -> dict:
    truth = scn["truth"]
    record = row.get("record") or {}
    service = str(record.get("affected_service", "")).strip().lower()
    started = parse_ts(record.get("started_at_utc"))
    try:
        transcript = json.loads((AGENT / "transcripts" / f"{row['run_id']}.json").read_text())
    except Exception:
        transcript = []
    quotes = check_quotes(row.get("text", ""), transcript)
    try:
        j = judge(scn, row.get("text", ""), record)
    except Exception as exc:
        j = {"cause_correct": None, "fix_acceptable": None, "wrongly_blamed": "", "notes": f"judge failed: {exc}"}
    checks = {
        "service": service in truth["services"],
        "start_time": started is not None and abs(started - t_truth) <= START_TOLERANCE,
        "cause": j.get("cause_correct"),
        "fix": j.get("fix_acceptable"),
        "quotes": quotes["checked"] > 0 and not quotes["missing"],
    }
    return {
        "checks": checks,
        "all_pass": all(v is True for v in checks.values()),
        "start_error_seconds": round(started - t_truth) if started is not None else None,
        "quotes_checked": quotes["checked"], "quotes_missing": quotes["missing"],
        "wrongly_blamed": j.get("wrongly_blamed", ""), "judge_notes": j.get("notes", ""),
    }


# --------------------------------------------------------------------- main

def run_one(cfg_name: str, scn: dict, repeat: int) -> dict:
    cfg = CONFIGS[cfg_name]
    run_id = f"{scn['id']}--{cfg_name.replace(' ', '_')}--r{repeat}--{int(time.time())}"
    base = {"run_id": run_id, "scenario": scn["id"], "title": scn["title"], "alert": scn["alert"],
            "kind": scn["kind"], "config": cfg_name, "model": cfg["model"],
            "disabled_tools": cfg["disabled_tools"], "repeat": repeat,
            "cause_determinable": scn["truth"]["cause_determinable"]}
    say(f"[{cfg_name}] {scn['id']} r{repeat}: reset")
    reset()
    (AGENT / "override.json").write_text(json.dumps({**cfg, "run_id": run_id, "quiet": True}))
    time.sleep(BASELINE_SECONDS)
    t_inject = time.time()
    out = sh(scn["inject"])
    marks = re.findall(r"T0=(\d+)", out)
    t_truth = float(marks[-1]) if marks else t_inject
    say(f"  injected; waiting for the write-up")
    row = wait_writeup(run_id)
    if row is None:
        result = {**base, "status": "no_writeup", "t_inject": t_inject, "t_truth": t_truth,
                  "checks": {k: False for k in ("service", "start_time", "cause", "fix", "quotes")}, "all_pass": False}
    else:
        alert_at = parse_ts(row.get("alert_started"))
        result = {**base, "status": "ok", "alert_fired": row.get("alert"), "t_inject": t_inject, "t_truth": t_truth,
                  "alert_delay_seconds": round(alert_at - t_truth) if alert_at else None,
                  **{k: row.get(k) for k in ("tool_calls", "seconds", "llm_calls", "input_tokens",
                                             "cached_input_tokens", "output_tokens", "cost_usd", "record", "text")},
                  **grade(scn, row, t_truth)}
        say(f"  {result['checks']}  ${result.get('cost_usd')}  {result.get('seconds')}s")
    with RESULTS.open("a") as f:
        f.write(json.dumps(result) + "\n")
    cleanup(scn)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--only", default="")
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()
    scenarios = yaml.safe_load((HERE / "scenarios.yaml").read_text())
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in args.only.split(",")]
    try:
        for cfg_name in [c.strip() for c in args.configs.split(",") if c.strip()]:
            for repeat in range(1, args.repeats + 1):
                for scn in scenarios:
                    try:
                        run_one(cfg_name, scn, repeat)
                    except Exception as exc:
                        say(f"  run failed: {type(exc).__name__}: {exc}")
                        try:
                            cleanup(scn)
                        except Exception:
                            pass
    finally:
        (AGENT / "override.json").unlink(missing_ok=True)   # live alerts go back to normal settings
        say("done")


if __name__ == "__main__":
    main()
