"""Створення задач з фіксованою інтенсивністю (відкритий контур): POST /lab-results/{id}/tasks."""
import argparse
import csv
import http.client
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://127.0.0.1:8080")
p.add_argument("--rate", type=float, required=True, help="задач за секунду")
p.add_argument("--count", type=int, default=0, help="скільки задач створити")
p.add_argument("--duration", type=float, default=0, help="або тривалість, с")
p.add_argument("--label", default=None)
a = p.parse_args()
n = a.count or int(a.rate * a.duration)
label = a.label or f"tasks_r{a.rate:g}_n{n}"
u = urlparse(a.url)
host, port = u.hostname, u.port or 80

c = http.client.HTTPConnection(host, port, timeout=10)
c.request("GET", "/lab-results?limit=100")
ids = [it["id"] for it in json.loads(c.getresponse().read())["items"]]
c.close()
if len(ids) < 100:
    raise SystemExit(f"У базі {len(ids)} записів, потрібно 100. Спершу виконай seed.")

try:
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass

local = threading.local()
records = []


def one(scheduled, rid):
    sent = time.perf_counter()
    status, task_id = 0, ""
    c = getattr(local, "conn", None) or http.client.HTTPConnection(host, port, timeout=10)
    local.conn = c
    try:
        c.request("POST", f"/lab-results/{rid}/tasks")
        r = c.getresponse()
        body = r.read()
        status = r.status
        if status == 202:
            task_id = json.loads(body)["id"]
    except Exception:
        c.close()
        local.conn = None
    records.append((scheduled - t0, sent - t0, time.perf_counter() - t0, status, rid, task_id))


t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=64) as pool:
    for i in range(n):
        scheduled = t0 + i / a.rate
        delay = scheduled - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        pool.submit(one, scheduled, random.choice(ids))

out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "tasks")
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, f"{label}.csv"), "w", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["scheduled_s", "sent_s", "done_s", "status", "record_id", "task_id"])
    for r in sorted(records):
        wr.writerow([f"{r[0]:.6f}", f"{r[1]:.6f}", f"{r[2]:.6f}", r[3], r[4], r[5]])

ok = [r[5] for r in records if r[3] == 202]
print(f"{label}: задано {a.rate:g} задач/с, надіслано {len(records)} за {max(r[1] for r in records):.1f} с, "
      f"створено (202) {len(ok)}, не 202: {len(records) - len(ok)}")
if ok:
    print(f"id створених задач: від {min(ok)} до {max(ok)} (повний список у results/tasks/{label}.csv)")