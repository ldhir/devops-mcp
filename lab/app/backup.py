"""backup: stand-in for a scheduled database dump job.

Every minute it writes one dump to /data/backups, verifies it, and deletes the
older ones. If /tmp/break_verify exists (a seeded failure), verification fails
every time, the job retries at once, and each failed attempt leaves its partial
dump behind.
"""
import glob, json, os, shutil, time

DIR, FLAG, SIZE_MB = "/data/backups", "/tmp/break_verify", 100


def emit(event, **fields):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "service": "backup", "event": event, **fields}), flush=True)


emit("backup_job_started", schedule="every 60s", keep=1, target=DIR)
attempt = 0
while True:
    os.makedirs(DIR, exist_ok=True)
    broken = os.path.exists(FLAG)
    attempt += 1
    path = f"{DIR}/shop-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{attempt}.archive"
    if shutil.disk_usage(DIR).free < (SIZE_MB + 60) * 1024 * 1024:
        emit("backup_failed", file=path, error="No space left on device", action="retrying")
        time.sleep(5 if broken else 60)
        continue
    with open(path, "wb") as f:
        for _ in range(SIZE_MB):
            f.write(os.urandom(1024 * 1024))
    if broken:
        emit("backup_verify_failed", file=path, size_mb=SIZE_MB, error="checksum mismatch",
             action="retrying in 5s", attempt=attempt)
        time.sleep(5)
        continue
    for old in glob.glob(f"{DIR}/*.archive"):
        if old != path:
            os.remove(old)
    emit("backup_completed", file=path, size_mb=SIZE_MB, kept=1)
    time.sleep(60)
