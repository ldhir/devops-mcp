"""Steady fake traffic so the lab always has metrics and logs."""
import os, random, time
import httpx

target = os.environ.get("TARGET", "http://nginx")
while True:
    phone = f"+1415555{random.randint(0, 9999):04d}"   # 555 numbers: fake by design
    try:
        httpx.post(f"{target}/orders", json={"phone": phone, "amount": random.randint(5, 200)}, timeout=5)
    except Exception:
        pass
    time.sleep(0.2)
