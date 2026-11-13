"""Follow-up questions in Slack.

An engineer replies in an alert thread and mentions the bot. The bot loads the saved
investigation for that thread, answers with the same read-only tools, and replies in
the thread. It uses Slack's Socket Mode: this machine opens the connection to Slack,
so nothing has to be reachable from the internet.

Limits, on purpose:
- It only answers when mentioned, and only in a thread under one of its own write-ups.
- Same tool allowlist, masking and audit log as the alert runner. Still read-only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from prompts import FOLLOWUP_RULES
from tools import HERE, call_tool, redact

log = logging.getLogger("followup")
THREADS = HERE / "threads"
MAX_STEPS = 10


def save_thread(thread_ts: str, model: str, messages: list) -> None:
    """Called by the runner after a write-up is posted, so follow-ups have its context."""
    THREADS.mkdir(exist_ok=True)
    (THREADS / f"{thread_ts}.json").write_text(json.dumps({"model": model, "messages": messages}, default=str))


def answer(client, tools: list, event: dict) -> None:
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    question = redact(re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip())
    path = THREADS / f"{thread_ts}.json"
    if not path.exists():
        _reply(channel, thread_ts, "I can only answer in a thread under one of my alert write-ups.")
        return
    if not question:
        return
    saved = json.loads(path.read_text())
    model, messages = saved["model"], saved["messages"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    messages.append({"role": "user", "content": f"{FOLLOWUP_RULES}\n\nTime now: {now}\nQuestion: {question}"})
    started, steps, reply = time.time(), 0, None
    try:
        while reply is None:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                tool_choice="none" if steps >= MAX_STEPS else "auto")
            msg = resp.choices[0].message
            if not msg.tool_calls:
                reply = redact(msg.content or "(no answer)")
                messages.append({"role": "assistant", "content": reply})
                break
            messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                steps += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if steps > MAX_STEPS or tc.function.name == "post_update":
                    result = json.dumps({"error": "not available; answer with what you have"})
                else:
                    result = call_tool(tc.function.name, args, caller="followup")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        footer = f"_{steps} tool calls · {time.time() - started:.0f}s · {model}_"
        _reply(channel, thread_ts, reply + "\n\n" + footer)
        path.write_text(json.dumps({"model": model, "messages": messages}, default=str))
        log.info("follow-up answered in thread %s after %d tool calls", thread_ts, steps)
    except Exception as exc:
        log.exception("follow-up failed")
        _reply(channel, thread_ts, f":warning: Could not answer: `{type(exc).__name__}`. See runner logs.")


def _reply(channel: str, thread_ts: str, text: str) -> None:
    import httpx
    r = httpx.post("https://slack.com/api/chat.postMessage", timeout=20,
                   headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
                   json={"channel": channel, "thread_ts": thread_ts, "text": text, "unfurl_links": False})
    if not r.json().get("ok"):
        log.error("Slack reply failed: %s", r.json().get("error"))


def start(client, tools: list) -> bool:
    """Open the Socket Mode connection in the background. Returns False if it is not configured."""
    token = os.environ.get("SLACK_APP_TOKEN", "")
    if not token:
        log.info("SLACK_APP_TOKEN not set; follow-up questions are off")
        return False
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    seen: set[str] = set()

    def handle(sm: SocketModeClient, req: SocketModeRequest) -> None:
        sm.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))  # ack first; Slack retries otherwise
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event", {})
        if event.get("type") != "app_mention" or event.get("bot_id"):
            return
        key = event.get("client_msg_id") or event.get("ts", "")
        if key in seen:
            return
        seen.add(key)
        threading.Thread(target=answer, args=(client, tools, event), daemon=True).start()

    sm = SocketModeClient(app_token=token)
    sm.socket_mode_request_listeners.append(handle)
    sm.connect()
    log.info("follow-up questions are on (Socket Mode connected)")
    return True
