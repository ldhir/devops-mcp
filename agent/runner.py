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

import followup
from prompts import RECORD_PROMPT, REVIEW_RULES, SYSTEM
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


def cost_usd(usage: dict, model: str) -> float | None:
    """Dollar cost of one investigation from the price table in config.yaml."""
    price = (CONFIG.get("prices") or {}).get(model)
    if not price:
        return None
    fresh = usage["input_tokens"] - usage["cached_input_tokens"]
    return round((fresh * price["input"] + usage["cached_input_tokens"] * price["cached_input"]
                  + usage["output_tokens"] * price["output"]) / 1e6, 4)


def review_messages(mode: str, messages: list, alert_text: str, transcript: list, draft: str) -> list:
    """Messages for the pre-post review. 'same' continues the investigator's own conversation;
    'fresh' gives a new context only the alert, the tool calls and results, and the draft."""
    if mode == "same":
        return messages + [{"role": "user", "content": REVIEW_RULES + "\n\nThe draft is your last message."}]
    calls = "\n\n".join(
        f"TOOL CALL {i}: {t['tool']} {json.dumps(t['args'])}\nRESULT: {t['result']}" for i, t in enumerate(transcript, 1))
    return [
        {"role": "system", "content": "You are a careful reviewer of incident write-ups for a DevOps team.\n\n" + REVIEW_RULES},
        {"role": "user", "content": f"ALERT:\n{alert_text}\n\nTOOL CALLS AND RESULTS:\n{calls}\n\nDRAFT WRITE-UP:\n{draft}"},
    ]


def run_review(model: str, msgs: list, draft: str, usage: dict) -> tuple[str, dict]:
    """One review call. Returns (final text, review summary). Falls back to the draft on any error."""
    try:
        resp = client.chat.completions.create(model=model, messages=msgs, response_format={"type": "json_object"})
        add_usage(usage, resp)
        rv = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:
        log.exception("review failed; posting the draft")
        return draft, {"verdict": "error", "error": f"{type(exc).__name__}: {exc}"}
    verdict = rv.get("verdict")
    text = redact((rv.get("writeup") or "").strip())
    changed = verdict in ("revise", "not_determined") and len(text) > 80
    summary = {"verdict": verdict if (verdict == "keep" or changed) else "keep",
               "tests": rv.get("tests"), "unsupported_claims": rv.get("unsupported_claims") or []}
    return (text if changed else draft), summary


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


def tool_loop(name: str, model: str, messages: list, tools: list, disabled: set, usage: dict) -> tuple[str, int, list]:
    """Let the model call tools until it writes its answer. Returns (answer, tool calls made, transcript)."""
    steps, transcript = 0, []
    while True:
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
            return final, steps, transcript
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


def second_pass(name: str, mode: str, model: str, messages: list, transcript: list, draft: str,
                usage: dict) -> tuple[str, dict | None, str]:
    """Check the draft against the evidence before anything is posted.
    Returns (final text, review summary, flag line for Slack).

    "flag" is the mode in use. It never changes the write-up: replaying a rewriting review over
    21 saved runs broke as many correct answers as it fixed. A failed timing, mechanism or size
    test is reliable enough to warn the reader, so that is all it does. The "fresh" and "same"
    modes rewrite the draft and are kept for evals/replay_review.py.
    """
    alert_text = messages[1]["content"]
    if mode == "flag":
        _, review = run_review(model, review_messages("fresh", messages, alert_text, transcript, draft), draft, usage)
        failed = {k: v.get("why", "") for k, v in (review.get("tests") or {}).items()
                  if isinstance(v, dict) and v.get("pass") is False}
        review["double_check"] = bool(failed) or review.get("verdict") == "not_determined"
        if not review["double_check"]:
            return draft, review, ""
        why = " ".join(f"{k.capitalize()}: {w}" for k, w in failed.items())[:420]
        log.info("%s: write-up flagged for double-check (%s)", name, ", ".join(failed) or "not determined")
        return draft, review, (":warning: *Double-check this one.* A second pass over the same evidence could not "
                               "confirm the cause." + (f" {why}" if why else ""))
    if mode in ("fresh", "same"):
        final, review = run_review(model, review_messages(mode, messages, alert_text, transcript, draft), draft, usage)
        if final != draft:
            log.info("%s: review verdict %s; write-up replaced", name, review.get("verdict"))
            messages.append({"role": "user", "content":
                             "A reviewer checked your draft against the tool results and replaced it. "
                             "The final write-up is:\n\n" + final})
            messages.append({"role": "assistant", "content": "Understood. That is the final write-up."})
        return final, review, ""
    return draft, None, ""


def structured_record(model: str, messages: list, usage: dict) -> dict | None:
    """A small structured copy of the conclusion, so code can grade it."""
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages + [{"role": "user", "content": RECORD_PROMPT}],
            response_format={"type": "json_object"})
        add_usage(usage, resp)
        return json.loads(redact(resp.choices[0].message.content or "{}"))
    except Exception:
        log.exception("could not build structured record")
        return None


def investigate(alert: dict) -> None:
    """One alert, start to finish: post it, investigate, check, post the write-up, save the run."""
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
        payload = {k: alert.get(k) for k in ("status", "labels", "annotations", "startsAt", "values")}
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Time now: {now}\nAlert payload from Grafana:\n" + redact(json.dumps(payload, default=str))},
        ]
        started = time.time()
        usage = {"llm_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}

        draft, steps, transcript = tool_loop(name, model, messages, tools, disabled, usage)
        mode = over.get("review") or MODEL.get("review") or "off"
        final, review, flag = second_pass(name, mode, model, messages, transcript, draft, usage)
        seconds = round(time.time() - started, 1)  # alert to write-up; excludes the record step below
        record = structured_record(model, messages, usage)
        cost = cost_usd(usage, model)

        if not quiet:
            if over.get("hold_seconds"):   # demo recordings: leave the bare alert on screen for a moment
                time.sleep(float(over["hold_seconds"]))
            footer = f"_{steps} tool calls · {seconds:.0f}s · {usage['input_tokens'] + usage['output_tokens']:,} tokens"
            footer += f" · ${cost:.2f} · {model}_" if cost is not None else f" · {model}_"
            post_update(final + ("\n\n" + flag if flag else "") + "\n\n" + footer, thread_ts=thread)
            try:  # keep the conversation so follow-up questions in this thread have its context
                followup.save_thread(thread, model, messages)
            except Exception:
                log.exception("could not save thread for follow-ups")

        run_id = over.get("run_id") or f"live-{int(started)}"
        (HERE / "transcripts").mkdir(exist_ok=True)
        (HERE / "transcripts" / f"{run_id}.json").write_text(json.dumps(transcript))
        with (HERE / "writeups.jsonl").open("a") as f:
            f.write(json.dumps({
                "ts": now, "run_id": run_id, "alert": name, "labels": alert.get("labels", {}),
                "alert_started": alert.get("startsAt"), "model": model,
                "disabled_tools": sorted(disabled), "tool_calls": steps, "seconds": seconds,
                **usage, "cost_usd": cost, "record": record, "text": final,
                "review": review, "draft_text": draft if draft != final else None}) + "\n")
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


@app.on_event("startup")
def start_followups() -> None:
    try:
        followup.start(client, AGENT_TOOLS)
    except Exception:
        log.exception("could not start follow-up listener")


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
