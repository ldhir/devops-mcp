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
- Find when it started. Look at the trend over a window of about an hour first and widen only if \
the change began before it. The start is when the trend changed (change_began), not the time of the \
first error line: errors come after the cause, and old log lines may belong to an earlier incident.
- Find what changed at that time. Use what_changed on the affected service and on the services \
around it. A deploy shortly before the start is a suspect, not proof: read its diff and check that \
the errors match the code that changed. Then look at logs from the services involved around the start time, \
especially warnings, errors, config changes, restarts and deploys. Routine request lines are noise; \
filter them out.
- The host may have had earlier, unrelated incidents. Anchor on this alert: find the most recent \
change point before it fired and investigate that one.
- A query that returns nothing is not evidence. Check the label names and try again.
- Follow the chain until you reach a cause a person could act on. Do not stop at the symptom.
- Only state what the tool results show. If you could not verify something, say so.

Before you name a cause, test it. A suspect has to pass all three:
- Timing: it changed shortly before the start, and it was still in effect when the problem began. \
A deploy that was replaced before the start was not running. A service doing what it always does \
did not change.
- Mechanism: a log line, a diff or a metric shows how it produces this symptom.
- Size: it accounts for the amount. For a filling disk, use disk_usage_breakdown and compare what \
the suspect wrote with how much the disk grew. A writer whose own logs show it behaving normally \
(for example keeping one file of a fixed size) cannot explain growth many times larger.
Do not claim something a tool result contradicts. Look at the services that write to or call the \
affected one, not only the affected one itself.

If no suspect passes, the cause is not determined, and that is a valid answer. Say what you \
established, say what you ruled out and why, say plainly that the cause could not be determined \
from the available data, and make the first step of the proposed fix the check a person should run \
to find it (for example listing the largest files on the disk, or asking who stopped the service). \
Do not present a guess as the cause.

Confidence: high only when timing, mechanism and size are all shown by tool results. Medium when \
one of them is inferred. Low when the cause is not determined.

You are read-only. You cannot change anything; you propose.

When done, reply with the Slack write-up and nothing else. Slack formatting: *bold* with single \
asterisks, `code` with backticks, no # headings, no tables. Use exactly these labelled lines:

*Affected:* service and host
*Started:* UTC time the problem began, and how fast it is moving
*Root cause:* one or two sentences
*Evidence:* 2-4 bullets, each with a number or a quoted log line and its time
*Proposed fix:* concrete steps, immediate first, then cleanup; mention any risk. If a deploy \
caused it, name the commit to roll back to
*Confidence:* high / medium / low, plus anything you could not verify

Keep it under 220 words. Times in UTC."""


def cost_usd(usage: dict, model: str) -> float | None:
    """Dollar cost of one investigation from the price table in config.yaml."""
    price = (CONFIG.get("prices") or {}).get(model)
    if not price:
        return None
    fresh = usage["input_tokens"] - usage["cached_input_tokens"]
    return round((fresh * price["input"] + usage["cached_input_tokens"] * price["cached_input"]
                  + usage["output_tokens"] * price["output"]) / 1e6, 4)


RECORD_PROMPT = """Now restate your conclusion as one JSON object and nothing else, with exactly these keys:
"affected_service": the single service at the centre of the problem, as named in the service map,
"started_at_utc": when the problem began, as "YYYY-MM-DDTHH:MM:SSZ", or null if unknown,
"root_cause": one sentence,
"proposed_fix": one sentence,
"cause_determined": true if the evidence shows the cause, false if you could not determine it,
"confidence": "high", "medium" or "low"."""


def load_override() -> dict:
    """Optional per-run settings written by the eval harness (model, disabled tools, run id)."""
    path = HERE / "override.json"
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def add_usage(usage: dict, resp) -> None:
    if not resp.usage:
        return
    usage["llm_calls"] += 1
    usage["input_tokens"] += resp.usage.prompt_tokens or 0
    usage["output_tokens"] += resp.usage.completion_tokens or 0
    details = getattr(resp.usage, "prompt_tokens_details", None)
    usage["cached_input_tokens"] += (getattr(details, "cached_tokens", 0) or 0) if details else 0


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
    over = load_override()
    model = over.get("model") or MODEL["name"]
    disabled = set(over.get("disabled_tools") or [])
    quiet = bool(over.get("quiet"))  # eval runs do not post to Slack
    tools = [t for t in AGENT_TOOLS if t["function"]["name"] not in disabled]
    thread = ""
    if not quiet:
        try:
            thread = post_update(describe(alert))["ts"]
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
        started = time.time()
        usage = {"llm_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        transcript = []
        final = None
        while final is None:
            force_answer = steps >= MODEL["max_steps"]
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                tool_choice="none" if force_answer else "auto",
            )
            msg = resp.choices[0].message
            add_usage(usage, resp)
            if not msg.tool_calls:
                final = redact(msg.content or "(no findings returned)")
                messages.append({"role": "assistant", "content": final})
                break
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
                if tc.function.name == "post_update" or tc.function.name in disabled:
                    result = json.dumps({"error": "not available to the investigator"})
                else:
                    result = call_tool(tc.function.name, args, caller="runner")
                transcript.append({"tool": tc.function.name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        seconds = round(time.time() - started, 1)  # alert to write-up; excludes the record step below

        # A small structured copy of the conclusion, so code can grade it.
        record = None
        try:
            messages.append({"role": "user", "content": RECORD_PROMPT})
            resp = client.chat.completions.create(
                model=model, messages=messages, response_format={"type": "json_object"})
            add_usage(usage, resp)
            record = json.loads(redact(resp.choices[0].message.content or "{}"))
        except Exception:
            log.exception("could not build structured record")

        cost = cost_usd(usage, model)
        if not quiet:
            footer = f"_{steps} tool calls · {seconds:.0f}s · {usage['input_tokens'] + usage['output_tokens']:,} tokens"
            footer += f" · ${cost:.2f} · {model}_" if cost is not None else f" · {model}_"
            post_update(final + "\n\n" + footer, thread_ts=thread)
        run_id = over.get("run_id") or f"live-{int(started)}"
        (HERE / "transcripts").mkdir(exist_ok=True)
        (HERE / "transcripts" / f"{run_id}.json").write_text(json.dumps(transcript))
        with (HERE / "writeups.jsonl").open("a") as f:
            f.write(json.dumps({
                "ts": now, "run_id": run_id, "alert": name, "labels": alert.get("labels", {}),
                "alert_started": alert.get("startsAt"), "model": model,
                "disabled_tools": sorted(disabled), "tool_calls": steps, "seconds": seconds,
                **usage, "cost_usd": cost, "record": record, "text": final}) + "\n")
        log.info("%s: write-up done after %d tool calls, %ss, cost %s", name, steps, seconds, cost)
    except Exception as exc:
        log.exception("investigation failed")
        if not quiet:
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
