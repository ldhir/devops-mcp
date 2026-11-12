"""Alert runner. Grafana calls POST /alert when an alert fires.

For each firing alert:
  1. post the plain alert to Slack, the way Grafana would,
  2. let the model investigate with the read-only tools,
  3. post its findings as a reply in that thread.

Run:  .venv/bin/uvicorn runner:app --host 0.0.0.0 --port 8088
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from openai import OpenAI

from tools import CONFIG, HERE, TOOLS, call_tool, post_update, redact

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("runner")

MODEL = CONFIG["model"]
if MODEL["provider"] != "openai":
    raise SystemExit(f"provider '{MODEL['provider']}' is not implemented yet")
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=MODEL.get("base_url") or None)

# The model investigates with read tools only. Posting to Slack is done by
# this code, so the model cannot post anything except its final write-up.
AGENT_TOOLS = [
    {"type": "function", "function": {"name": n, "description": s["description"], "parameters": s["parameters"]}}
    for n, s in TOOLS.items()
    if n != "post_update"
]

SYSTEM = """You are the on-call investigator for a DevOps team. A monitoring alert has fired. \
The alert only names a symptom (for example a full disk or an unreachable instance). Your job is \
to find the root cause using the tools, then write it up for the engineers in Slack.

How to investigate:
- Turn the host and mountpoint into a service with service_map.
- Confirm the symptom with numbers, then find what exactly is growing or failing, as narrowly as \
the data allows (which database, which collection, which process).
- Find when it started. Look at the trend over a long enough window to see the change point.
- Find what changed at that time: look at logs from the services involved around the start time, \
especially warnings, errors, config changes, restarts and deploys. Routine request lines are noise; \
filter them out.
- The host may have had earlier, unrelated incidents. Anchor on this alert: find the most recent \
change point before it fired and investigate that one.
- A query that returns nothing is not evidence. Check the label names and try again.
- Follow the chain until you reach a cause a person could act on. Do not stop at the symptom.
- Only state what the tool results show. If you could not verify something, say so.

You are read-only. You cannot change anything; you propose.

When done, reply with the Slack write-up and nothing else. Slack formatting: *bold* with single \
asterisks, `code` with backticks, no # headings, no tables. Use exactly these labelled lines:

*Affected:* service and host
*Started:* UTC time the problem began, and how fast it is moving
*Root cause:* one or two sentences
*Evidence:* 2-4 bullets, each with a number or a quoted log line and its time
*Proposed fix:* concrete steps, immediate first, then cleanup; mention any risk
*Confidence:* high / medium / low, plus anything you could not verify

Keep it under 220 words. Times in UTC."""


def describe(alert: dict) -> str:
    labels = alert.get("labels", {})
    name = labels.get("alertname", "alert")
    parts = [f"{k}={v}" for k, v in labels.items() if k in ("instance", "mountpoint", "job", "service", "device")]
    values = alert.get("values") or {}
    val = ", ".join(f"{k}={round(v, 1) if isinstance(v, (int, float)) else v}" for k, v in values.items())
    summary = (alert.get("annotations") or {}).get("summary", "")
    text = f":red_circle: *[FIRING] {name}*"
    if summary:
        text += f"\n{summary}"
    if parts:
        text += "\n" + "  ".join(f"`{p}`" for p in parts)
    if val:
        text += f"\nvalue: {val}"
    return text


def investigate(alert: dict) -> None:
    name = alert.get("labels", {}).get("alertname", "alert")
    try:
        root = post_update(describe(alert))
        thread = root["ts"]
    except Exception:
        log.exception("could not post alert to Slack")
        return
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": f"Time now: {now}\nAlert payload from Grafana:\n"
                + redact(json.dumps({k: alert.get(k) for k in ("status", "labels", "annotations", "startsAt", "values")}, default=str)),
            },
        ]
        steps = 0
        while True:
            force_answer = steps >= MODEL["max_steps"]
            resp = client.chat.completions.create(
                model=MODEL["name"],
                messages=messages,
                tools=AGENT_TOOLS,
                tool_choice="none" if force_answer else "auto",
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                final = redact(msg.content or "(no findings returned)")
                post_update(final, thread_ts=thread)
                with (HERE / "writeups.jsonl").open("a") as f:
                    f.write(json.dumps({"ts": now, "alert": name, "tool_calls": steps, "text": final}) + "\n")
                log.info("%s: write-up posted after %d tool calls", name, steps)
                return
            messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                steps += 1
                if steps > MODEL["max_steps"]:  # several calls in one turn can overshoot the cap
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": '{"error": "step limit reached; write up what you have"}'})
                    continue
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("%s: step %d %s %s", name, steps, tc.function.name, json.dumps(args)[:200])
                result = call_tool(tc.function.name, args, caller="runner") if tc.function.name != "post_update" \
                    else json.dumps({"error": "not available to the investigator"})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if force_answer:
                # tool_choice none should prevent this; stop rather than loop
                post_update("Investigation hit its step limit without a conclusion.", thread_ts=thread)
                return
    except Exception as exc:
        log.exception("investigation failed")
        try:
            post_update(f":warning: Investigation failed: `{type(exc).__name__}`. See runner logs.", thread_ts=thread)
        except Exception:
            pass


app = FastAPI()
_seen: dict[str, float] = {}
_lock = threading.Lock()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": MODEL["name"]}


@app.post("/alert")
async def alert_hook(request: Request) -> dict:
    body = await request.json()
    started = 0
    for alert in body.get("alerts", []):
        key = alert.get("fingerprint") or json.dumps(alert.get("labels", {}), sort_keys=True)
        if alert.get("status") != "firing":
            with _lock:
                _seen.pop(key, None)  # resolved: the next firing is a new incident
            continue
        with _lock:
            last = _seen.get(key, 0)
            if time.time() - last < CONFIG["runner"]["dedupe_minutes"] * 60:
                continue
            _seen[key] = time.time()
        threading.Thread(target=investigate, args=(alert,), daemon=True).start()
        started += 1
    log.info("webhook: %d alerts, %d investigations started", len(body.get("alerts", [])), started)
    return {"ok": True, "started": started}
