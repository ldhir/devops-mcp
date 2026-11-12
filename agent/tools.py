"""Read-only tools shared by the MCP server and the alert runner.

Every call goes through call_tool(), which is the single place that
  1. checks the tool is on the allowlist,
  2. redacts phone numbers and emails from the result, and
  3. appends a line to the audit log.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).parent
CONFIG = yaml.safe_load((HERE / "config.yaml").read_text())
PROM = CONFIG["sources"]["prometheus"]
LOKI = CONFIG["sources"]["loki"]
JENKINS = CONFIG["sources"].get("jenkins", "")
AUDIT_PATH = HERE / CONFIG.get("audit_log", "audit.jsonl")
MAX_RESULT_CHARS = 7000


def load_env() -> None:
    """Read KEY=VALUE lines from .env next to this file into os.environ."""
    env = HERE / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

# ---------------------------------------------------------------- redaction

# Known limit: a bare run of digits with no "+" and no separators is not
# masked, because it cannot be told apart from an epoch timestamp or an id.
_PHONE_PLUS = re.compile(r"\+\d{10,15}")
_PHONE_SEP = re.compile(r"(?<!\d)(?:1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact(text: str) -> str:
    text = _PHONE_PLUS.sub("[PHONE]", text)
    text = _PHONE_SEP.sub("[PHONE]", text)
    return _EMAIL.sub("[EMAIL]", text)


# ------------------------------------------------------------------ helpers

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prom(path: str, params: dict) -> dict:
    r = httpx.get(f"{PROM}{path}", params=params, timeout=20)
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {body.get('error', r.text[:300])}")
    return body["data"]


def _instant(promql: str) -> list[dict]:
    out = []
    for s in _prom("/api/v1/query", {"query": promql})["result"]:
        labels = {k: v for k, v in s["metric"].items() if k != "__name__"}
        out.append({"labels": labels, "value": round(float(s["value"][1]), 3)})
    return out


# -------------------------------------------------------------------- tools

def service_map() -> dict:
    """Which services run on which host, what they depend on, which disk is whose."""
    return CONFIG["service_map"]


def host_health(host: str) -> dict:
    sel = f'instance="{host}"'
    fs = f'{sel},fstype=~"ext4|xfs"'
    used = f"100 * (1 - node_filesystem_avail_bytes{{{fs}}} / node_filesystem_size_bytes{{{fs}}})"
    return {
        "host": host,
        "now_utc": _iso(time.time()),
        "targets_up": _instant(f"up{{{sel}}}"),
        "disk_used_percent": _instant(used),
        "disk_used_percent_30m_ago": _instant(
            f"100 * (1 - node_filesystem_avail_bytes{{{fs}}} offset 30m"
            f" / node_filesystem_size_bytes{{{fs}}} offset 30m)"
        ),
        "disk_size_gb": _instant(f"node_filesystem_size_bytes{{{fs}}} / 1e9"),
        "memory_available_percent": _instant(
            f"100 * node_memory_MemAvailable_bytes{{{sel}}} / node_memory_MemTotal_bytes{{{sel}}}"
        ),
        "cpu_busy_percent_5m": _instant(
            f'100 * (1 - avg(rate(node_cpu_seconds_total{{{sel},mode="idle"}}[5m])))'
        ),
        "load1": _instant(f"node_load1{{{sel}}}"),
    }


def list_metrics(contains: str) -> dict:
    names = _prom("/api/v1/label/__name__/values", {})
    hits = [n for n in names if contains.lower() in n.lower()]
    return {"matching": len(hits), "names": hits[:60], "truncated": len(hits) > 60}


def _label_hint(promql: str) -> dict:
    """When a query matches nothing, show the labels each metric in it really has.
    A wrong label name (db vs database) otherwise looks the same as 'no data'."""
    hint = {}
    for name in dict.fromkeys(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]{5,}", promql)):
        try:
            found = _prom("/api/v1/series", {"match[]": name, "start": time.time() - 6 * 3600, "end": time.time()})
        except Exception:
            continue
        if found:
            values: dict[str, set] = {}
            for s in found[:50]:
                for k, v in s.items():
                    if k != "__name__":
                        values.setdefault(k, set()).add(v)
            hint[name] = {k: sorted(v)[:8] for k, v in values.items()}
        if len(hint) >= 3:
            break
    return hint


def query_metrics(promql: str, minutes_back: int = 0) -> dict:
    """minutes_back=0 gives the value now; otherwise the trend over that window."""
    if minutes_back <= 0:
        now = _instant(promql)[:25]
        out = {"now_utc": _iso(time.time()), "series": now}
        if not now:
            out["note"] = ("No series matched right now. The target may be down (try minutes_back>0 "
                           "for history) or a label name/value is wrong; real labels are below.")
            out["labels_seen_last_6h"] = _label_hint(promql)
        return out
    end = time.time()
    start = end - minutes_back * 60
    step = max(15, int(minutes_back * 60 / 40))
    data = _prom(
        "/api/v1/query_range",
        {"query": promql, "start": start, "end": end, "step": step},
    )
    series = []
    for s in data["result"][:20]:
        pts = [(float(t), float(v)) for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
        if not pts:
            continue
        lo = min(pts, key=lambda p: p[1])
        hi = max(pts, key=lambda p: p[1])
        stride = max(1, len(pts) // 20)
        series.append(
            {
                "labels": {k: v for k, v in s["metric"].items() if k != "__name__"},
                "first": [_iso(pts[0][0]), round(pts[0][1], 3)],
                "last": [_iso(pts[-1][0]), round(pts[-1][1], 3)],
                "min": [_iso(lo[0]), round(lo[1], 3)],
                "max": [_iso(hi[0]), round(hi[1], 3)],
                "points": [[_iso(t), round(v, 3)] for t, v in pts[::stride]],
            }
        )
    out = {"window_minutes": minutes_back, "step_seconds": step, "series": series}
    if not series:
        out["note"] = "No series matched. A label name or value is probably wrong; real labels are below."
        out["labels_seen_last_6h"] = _label_hint(promql)
    return out


_VARIABLE = re.compile(r"\b[0-9a-f]{8,}\b|\d+")


def log_summary(service: str, minutes_back: int = 60) -> dict:
    """Count log lines by kind, so one rare line is not buried under thousands of routine ones."""
    end = time.time()
    selector = f'{{service="{service}"}}'

    def loki(path: str, params: dict) -> list:
        r = httpx.get(f"{LOKI}{path}", params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Loki error {r.status_code}: {r.text[:300]}")
        return r.json()["data"]["result"]

    def first_line(query: str) -> str:
        res = loki("/loki/api/v1/query_range", {
            "query": query, "limit": 1, "direction": "forward",
            "start": int((end - minutes_back * 60) * 1e9), "end": int(end * 1e9)})
        for stream in res:
            for ts, line in stream["values"]:
                return f"{_iso(int(ts) / 1e9)} {line[:400]}"
        return ""

    # Structured (JSON) logs: group by the event and level fields.
    grouped = loki("/loki/api/v1/query", {
        "query": f'sum by (event, level) (count_over_time({selector} | json | __error__="" [{minutes_back}m]))',
        "time": int(end * 1e9)})
    kinds = []
    for g in grouped:
        event, level = g["metric"].get("event", ""), g["metric"].get("level", "")
        if not event and not level:
            continue
        kinds.append({"event": event, "level": level, "count": int(float(g["value"][1]))})
    if kinds:
        kinds.sort(key=lambda k: k["count"])
        for k in kinds[:12]:  # rarest kinds get their first occurrence attached
            q = selector + " | json"
            if k["event"]:
                q += f' | event={json.dumps(k["event"])}'
            if k["level"]:
                q += f' | level={json.dumps(k["level"])}'
            k["first_seen"] = first_line(q)
        out = {"service": service, "window_minutes": minutes_back, "format": "json",
               "kinds_rarest_first": kinds[:30]}
        # Lines that are not JSON (stack traces, startup errors) would be invisible above.
        plain = loki("/loki/api/v1/query_range", {
            "query": selector + ' | json | __error__!=""', "limit": 400, "direction": "backward",
            "start": int((end - minutes_back * 60) * 1e9), "end": int(end * 1e9)})
        shapes: dict[str, dict] = {}
        for stream in plain:
            for ts, line in stream["values"]:
                shape = _VARIABLE.sub("#", line)[:160]
                s = shapes.setdefault(shape, {"count": 0, "first_ts": int(ts), "sample": line[:300]})
                s["count"] += 1
                if int(ts) < s["first_ts"]:
                    s["first_ts"], s["sample"] = int(ts), line[:300]
        if shapes:
            top = sorted(shapes.values(), key=lambda s: -s["count"])[:12]
            out["non_json_lines"] = {
                "note": "unstructured lines such as stack traces; newest 400 sampled, most frequent first",
                "kinds": [{"count": s["count"], "first_seen": f'{_iso(s["first_ts"] / 1e9)} {s["sample"]}'} for s in top]}
        return out

    # Plain-text logs: mask numbers and ids, then count the resulting shapes.
    res = loki("/loki/api/v1/query_range", {
        "query": selector, "limit": 2000, "direction": "backward",
        "start": int((end - minutes_back * 60) * 1e9), "end": int(end * 1e9)})
    shapes: dict[str, dict] = {}
    total = 0
    for stream in res:
        for ts, line in stream["values"]:
            total += 1
            # Drop a leading timestamp, then mask variable parts.
            body = re.sub(r"^\S*\d{4}-\d{2}-\d{2}[T ]\S+\s*", "", line)
            shape = _VARIABLE.sub("#", body)[:160]
            s = shapes.setdefault(shape, {"count": 0, "first_ts": int(ts), "sample": line[:400]})
            s["count"] += 1
            if int(ts) < s["first_ts"]:
                s["first_ts"], s["sample"] = int(ts), line[:400]
    ordered = sorted(shapes.values(), key=lambda s: s["count"])[:30]
    return {"service": service, "window_minutes": minutes_back, "format": "text",
            "lines_sampled": total, "sampled_newest_only": total >= 2000,
            "kinds_rarest_first": [{"count": s["count"], "first_seen": f'{_iso(s["first_ts"] / 1e9)} {s["sample"]}'}
                                   for s in ordered]}


def get_logs(
    service: str = "",
    contains: str = "",
    exclude: str = "",
    minutes_back: int = 30,
    limit: int = 40,
    oldest_first: bool = False,
    logql: str = "",
) -> dict:
    if not logql:
        if not service:
            raise ValueError("give either service or logql")
        logql = f'{{service="{service}"}}'
        if contains:
            logql += f" |= {json.dumps(contains)}"
        if exclude:
            logql += f" != {json.dumps(exclude)}"
    end = time.time()
    r = httpx.get(
        f"{LOKI}/loki/api/v1/query_range",
        params={
            "query": logql,
            "start": int((end - minutes_back * 60) * 1e9),
            "end": int(end * 1e9),
            "limit": min(int(limit), 100),
            "direction": "forward" if oldest_first else "backward",
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Loki error {r.status_code}: {r.text[:300]}")
    lines = []
    for stream in r.json()["data"]["result"]:
        svc = stream["stream"].get("service", "")
        for ts, line in stream["values"]:
            lines.append((int(ts), svc, line[:600]))
    lines.sort(reverse=not oldest_first)
    return {
        "query": logql,
        "window_minutes": minutes_back,
        "returned": len(lines),
        "lines": [f"{_iso(ts / 1e9)} [{svc}] {line}" for ts, svc, line in lines],
    }


def _service_entry(service: str) -> dict:
    for host in CONFIG["service_map"].values():
        if service in host.get("services", {}):
            return host["services"][service]
    raise ValueError(f"unknown service '{service}'; see service_map")


def list_deploys(service: str, hours_back: int = 24) -> dict:
    """Deploy history for a service, from its Jenkins job. Newest first."""
    job = _service_entry(service).get("deploy_job")
    if not job:
        return {"service": service, "note": "no deploy job is recorded for this service; it is not deployed through Jenkins"}
    r = httpx.get(
        f"{JENKINS}/job/{job}/api/json",
        params={"tree": "builds[number,result,building,timestamp,duration,description,actions[parameters[name,value]]]{0,40}"},
        auth=(os.environ["JENKINS_USER"], os.environ["JENKINS_PASSWORD"]),
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Jenkins error {r.status_code}")
    cutoff = time.time() - hours_back * 3600
    inside, before = [], None
    for b in r.json().get("builds", []):
        started = b["timestamp"] / 1000
        desc = (b.get("description") or "").split(" ", 1)
        ref = next((p["value"] for a in b.get("actions", []) for p in a.get("parameters", []) if p.get("name") == "REF"), "")
        row = {
            "build": b["number"],
            "started_utc": _iso(started),
            "finished_utc": _iso(started + b.get("duration", 0) / 1000) if not b.get("building") else None,
            "result": "RUNNING" if b.get("building") else b.get("result"),
            "commit": desc[0] if desc[0] else None,
            "commit_subject": desc[1] if len(desc) > 1 else None,
            "requested_ref": ref,
        }
        if started >= cutoff:
            inside.append(row)
        elif before is None:
            before = row
    return {"service": service, "job": job, "window_hours": hours_back,
            "deploys_newest_first": inside, "last_deploy_before_window": before}


def get_diff(service: str, commit: str, base: str = "") -> dict:
    """What a commit changed (or everything between base and commit), from Bitbucket."""
    repo = _service_entry(service).get("repo")
    if not repo:
        raise ValueError(f"no repository is recorded for service '{service}'")
    api = f"https://api.bitbucket.org/2.0/repositories/{repo}"
    headers = {"Authorization": f"Bearer {os.environ['BITBUCKET_TOKEN']}"}
    spec = f"{commit}..{base}" if base else commit
    meta = httpx.get(f"{api}/commit/{commit}", headers=headers, timeout=20)
    if meta.status_code != 200:
        raise RuntimeError(f"Bitbucket error {meta.status_code} for commit {commit}")
    m = meta.json()
    diff = httpx.get(f"{api}/diff/{spec}", headers=headers, timeout=30, follow_redirects=True)
    if diff.status_code != 200:
        raise RuntimeError(f"Bitbucket error {diff.status_code} for diff {spec}")
    text = diff.text
    return {
        "service": service, "repo": repo, "commit": m["hash"][:7], "compared_to": base or "its parent",
        "date_utc": m.get("date"), "author": (m.get("author") or {}).get("user", {}).get("display_name")
        or (m.get("author") or {}).get("raw"), "message": (m.get("message") or "").strip(),
        "diff": text[:5000], "diff_truncated": len(text) > 5000,
    }


def what_changed(service: str, minutes_back: int = 120) -> dict:
    """Everything that changed for one service in a window: deploys, restarts, new kinds of log line."""
    entry = _service_entry(service)
    out: dict = {"service": service, "window_minutes": minutes_back, "now_utc": _iso(time.time()),
                 "depends_on": entry.get("depends_on", [])}
    try:
        d = list_deploys(service, hours_back=max(1, -(-minutes_back // 60)))
        cutoff = time.time() - minutes_back * 60
        if "deploys_newest_first" in d:
            out["deploys"] = [x for x in d["deploys_newest_first"]
                              if datetime.strptime(x["started_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() >= cutoff]
            older = [x for x in d["deploys_newest_first"] if x not in out["deploys"]]
            out["last_deploy_before_window"] = older[0] if older else d.get("last_deploy_before_window")
        else:
            out["deploys"] = d.get("note")
    except Exception as exc:
        out["deploys"] = f"unavailable: {exc}"
    job = entry.get("metrics_job")
    if job:
        try:
            out["scrape_target_up"] = query_metrics(f'up{{job="{job}"}}', minutes_back).get("series", [])
            out["running_version_over_window"] = [
                {"version": s["labels"].get("version"), "first_seen": s["first"][0], "last_seen": s["last"][0]}
                for s in query_metrics(f'orders_build_info{{job="{job}"}}', minutes_back).get("series", [])
            ] if service == "orders-api" else None
            out["process_restarts"] = _instant(f'changes(process_start_time_seconds{{job="{job}"}}[{minutes_back}m])')
        except Exception as exc:
            out["metrics"] = f"unavailable: {exc}"
        for key in ("scrape_target_up",):
            for s in out.get(key) or []:
                s.pop("points", None)
    try:
        out["log_kinds_rarest_first"] = log_summary(service, minutes_back).get("kinds_rarest_first", [])[:8]
    except Exception as exc:
        out["log_kinds_rarest_first"] = f"unavailable: {exc}"
    return out


def post_update(text: str, thread_ts: str = "") -> dict:
    """Post a message to the configured Slack channel. The only tool that writes anywhere."""
    payload = {"channel": CONFIG["slack"]["channel"], "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    r = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        json=payload,
        timeout=20,
    )
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Slack error: {body.get('error')}")
    return {"ok": True, "ts": body["ts"], "channel": body["channel"]}


# ----------------------------------------------------------------- registry

def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S, _I, _B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}

# The allowlist. A tool that is not in this dict cannot be called.
TOOLS: dict[str, dict] = {
    "service_map": {
        "fn": service_map,
        "description": "Which services run on which host, what each depends on, and which disk belongs to which service. Start here to turn a host or mountpoint into a service.",
        "parameters": _schema({}, []),
    },
    "host_health": {
        "fn": host_health,
        "description": "Snapshot of one host: which scrape targets are up, disk used percent per mountpoint now and 30 minutes ago, memory, CPU, load.",
        "parameters": _schema({"host": {**_S, "description": "Host name, e.g. the alert's instance label"}}, ["host"]),
    },
    "list_metrics": {
        "fn": list_metrics,
        "description": "Find Prometheus metric names containing a substring (case-insensitive). Use it to discover what can be queried, e.g. 'storageSize', 'mongodb_dbstats', 'orders_'.",
        "parameters": _schema({"contains": _S}, ["contains"]),
    },
    "query_metrics": {
        "fn": query_metrics,
        "description": "Run a PromQL query. minutes_back=0 returns current values. minutes_back>0 returns the trend over that window per series (first, last, min, max and sampled points with UTC times), which is how you find when something started changing.",
        "parameters": _schema({"promql": _S, "minutes_back": {**_I, "description": "0 for now, or window size in minutes"}}, ["promql"]),
    },
    "what_changed": {
        "fn": what_changed,
        "description": "Everything that changed for one service in a time window, in one call: deploys (from Jenkins), restarts, whether its scrape target was up, which version was running, and the rarest kinds of log line with their first occurrence. Call this early for the affected service and for the services it depends on or that depend on it.",
        "parameters": _schema({"service": _S, "minutes_back": _I}, ["service"]),
    },
    "list_deploys": {
        "fn": list_deploys,
        "description": "Deploy history for a service from its Jenkins job, newest first: build number, start and finish time (UTC), result, commit and commit subject. Also returns the last deploy before the window, which is the version to roll back to.",
        "parameters": _schema({"service": _S, "hours_back": _I}, ["service"]),
    },
    "get_diff": {
        "fn": get_diff,
        "description": "What a commit changed, from Bitbucket: message, author, date and the code diff. Pass 'base' (an older commit) to see everything between two deployed versions. A deploy near the start of a problem is a suspect; the diff is how you confirm or clear it.",
        "parameters": _schema({"service": _S, "commit": _S, "base": {**_S, "description": "optional older commit to compare against"}}, ["service", "commit"]),
    },
    "log_summary": {
        "fn": log_summary,
        "description": "Count a service's log lines by kind over a window, rarest first, with the first occurrence and time of each rare kind. Use this before get_logs: a single config change, warning or error line is easy to miss among thousands of routine request lines.",
        "parameters": _schema({"service": _S, "minutes_back": _I}, ["service"]),
    },
    "get_logs": {
        "fn": get_logs,
        "description": "Read logs from Loki, newest first unless oldest_first is true. Give a service name with optional 'contains' and 'exclude' text filters, or a raw LogQL query in 'logql'. High-volume routine lines can hide the interesting ones, so filter. Phone numbers and emails are masked.",
        "parameters": _schema(
            {
                "service": _S,
                "contains": _S,
                "exclude": _S,
                "minutes_back": _I,
                "limit": {**_I, "description": "max lines, up to 100"},
                "oldest_first": _B,
                "logql": {**_S, "description": "raw LogQL; overrides service/contains/exclude"},
            },
            [],
        ),
    },
    "post_update": {
        "fn": post_update,
        "description": "Post a message to the team's Slack alerts channel. Pass thread_ts to reply in a thread.",
        "parameters": _schema({"text": _S, "thread_ts": _S}, ["text"]),
    },
}


def call_tool(name: str, args: dict | None, caller: str) -> str:
    """Run one tool. Returns a redacted JSON string. Never raises."""
    args = args or {}
    started = time.time()
    ok = True
    try:
        if name not in TOOLS:
            raise ValueError(f"tool '{name}' is not on the allowlist")
        result = json.dumps(TOOLS[name]["fn"](**args), default=str)
    except Exception as exc:  # the model gets the error text and can adjust
        ok = False
        result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    result = redact(result)
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + '... [truncated: narrow the query]'
    entry = {
        "ts": _iso(started),
        "caller": caller,
        "tool": name,
        "args": json.loads(redact(json.dumps(args, default=str))),
        "ok": ok,
        "result_chars": len(result),
        "ms": int((time.time() - started) * 1000),
    }
    with AUDIT_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return result
