"""orders-api: a small stand-in for a typical backend service.

Writes orders to MongoDB and logs one JSON line per request. Log lines carry a
fake phone number on purpose, so redaction has something real to remove.
The debug-capture switch exists to seed a failure: when on, every request also
stores a large payload, which fills MongoDB's disk.
"""
import json, logging, os, random, sys, time
from fastapi import FastAPI, Response
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from pymongo import MongoClient
import redis

log = logging.getLogger("orders-api")
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")

mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=2000)
db = mongo["shop"]
cache = redis.Redis.from_url(os.environ["REDIS_URL"])

REQS = Counter("orders_requests_total", "Order requests", ["outcome"])
DEBUG_ON = Gauge("orders_debug_capture_enabled", "1 when debug payload capture is on")
app = FastAPI()


def emit(level, event, **fields):
    log.log(level, json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "service": "orders-api", "event": event, **fields}))


def debug_capture() -> bool:
    try:
        return cache.get("debug_capture") == b"1"
    except Exception:
        return False


@app.post("/orders")
def create_order(order: dict):
    phone = order.get("phone", "")
    try:
        db.orders.insert_one({"phone": phone, "amount": order.get("amount", 0), "ts": time.time()})
        if debug_capture():
            DEBUG_ON.set(1)
            db.audit_events.insert_one({"ts": time.time(), "phone": phone,
                                        "raw_payload": random.randbytes(1_500_000)})
        else:
            DEBUG_ON.set(0)
        REQS.labels("ok").inc()
        emit(logging.INFO, "order_created", phone=phone, amount=order.get("amount", 0))
        return {"ok": True}
    except Exception as exc:
        REQS.labels("error").inc()
        emit(logging.ERROR, "order_failed", phone=phone, error=str(exc)[:300])
        return Response(status_code=503, content="order failed")


@app.post("/admin/debug-capture/{state}")
def set_debug(state: str):
    on = state == "on"
    cache.set("debug_capture", "1" if on else "0")
    emit(logging.WARNING, "debug_capture_changed", enabled=on,
         note="full request payloads are written to shop.audit_events while enabled")
    return {"debug_capture": on}


@app.get("/health")
def health():
    mongo.admin.command("ping")
    return {"ok": True}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
