"""Воркер черги задач: захоплює задачу в PostgreSQL, обробляє синтетичний зріз, пише результат."""
import math
import os
import random
import time
import urllib.request

import psycopg
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DB = dict(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], dbname=os.environ["DB_NAME"],
          user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], connect_timeout=3)
WEB_URL = os.environ["WEB_URL"]
LEASE = float(os.environ["LEASE_SECONDS"])
POLL = float(os.environ["POLL_INTERVAL_MS"]) / 1000
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0"))
WORKER_ID = os.environ["WORKER_ID"]

S = 256
Q_LO, Q_HI = 0.02, 0.98

TASKS = Counter("worker_tasks_total", "Задачі за результатом", ["result"])
CLAIMS = Counter("worker_claims_total", "Спроби захоплення за результатом", ["result"])
STEP = Histogram("worker_task_duration_seconds", "Тривалість кроку задачі, с", ["step"],
                 buckets=[0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 1.024, 2.048])
IN_FLIGHT = Gauge("worker_tasks_in_flight", "Задач в обробці")

CLAIM = """
UPDATE tasks SET status = 'running', worker_id = %s, token = token + 1,
       lease_until = now() + make_interval(secs => %s), attempts = attempts + 1, started_at = now()
WHERE id = (SELECT id FROM tasks WHERE status = 'queued'
            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING id, record_id, seed, token
"""


def process(seed):
    n = S * S
    x = seed
    pixels = []
    hist = [0] * 4096
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        p = x >> 19
        pixels.append(p)
        hist[p] += 1

    lo = hi = None
    cum = 0
    for v in range(4096):
        cum += hist[v]
        if lo is None and cum >= Q_LO * n:
            lo = v
        if cum >= Q_HI * n:
            hi = v
            break

    span = (hi - lo) or 1
    removed = total = total2 = 0
    for p in pixels:
        if p < lo or p > hi:
            removed += 1
        else:
            q = int(4095 * (p - lo) / span + 0.5)
            total += q
            total2 += q * q
    kept = n - removed
    mean = total / kept
    std = math.sqrt(max(total2 / kept - mean * mean, 0))
    return {"p_lo": lo, "p_hi": hi, "removed": removed, "mean": round(mean, 4), "std": round(std, 4)}


start_http_server(9100)
print(f"{WORKER_ID}: старт, L={LEASE:g} с, T_poll={POLL * 1000:g} мс, FAIL_RATE={FAIL_RATE}", flush=True)

while True:
    try:
        with psycopg.connect(**DB, autocommit=True, row_factory=dict_row) as conn:
            while True:
                t = time.perf_counter()
                task = conn.execute(CLAIM, (WORKER_ID, LEASE)).fetchone()
                STEP.labels("claim").observe(time.perf_counter() - t)
                if task is None:
                    CLAIMS.labels("empty").inc()
                    time.sleep(POLL)
                    continue
                CLAIMS.labels("task").inc()
                IN_FLIGHT.inc()
                try:
                    t = time.perf_counter()
                    with urllib.request.urlopen(f"{WEB_URL}/lab-results/{task['record_id']}", timeout=5) as r:
                        r.read()
                    STEP.labels("fetch").observe(time.perf_counter() - t)

                    t = time.perf_counter()
                    result = process(task["seed"])
                    STEP.labels("process").observe(time.perf_counter() - t)

                    if random.random() < FAIL_RATE:
                        os._exit(1)

                    t = time.perf_counter()
                    with conn.transaction():
                        conn.execute(
                            "INSERT INTO task_results (task_id, token, worker_id, result) VALUES (%s, %s, %s, %s)",
                            (task["id"], task["token"], WORKER_ID, Jsonb(result)))
                        done = conn.execute(
                            "UPDATE tasks SET status = 'done', finished_at = now() WHERE id = %s AND token = %s",
                            (task["id"], task["token"])).rowcount
                        if not done:
                            raise psycopg.Rollback()
                    STEP.labels("store").observe(time.perf_counter() - t)
                    if done:
                        TASKS.labels("done").inc()
                    else:
                        TASKS.labels("lost").inc()
                        print(f"{WORKER_ID}: задача {task['id']}, токен {task['token']}: оновлено 0 рядків", flush=True)
                except psycopg.OperationalError:
                    raise
                except Exception as e:
                    TASKS.labels("failed").inc()
                    print(f"{WORKER_ID}: задача {task['id']} помилка: {e}", flush=True)
                finally:
                    IN_FLIGHT.dec()
    except psycopg.OperationalError as e:
        print(f"{WORKER_ID}: втрачено зʼєднання з базою ({e}), перепідключення", flush=True)
        time.sleep(1)