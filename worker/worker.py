"""Воркер черги задач: захоплює задачу в PostgreSQL, обробляє синтетичний зріз, пише результат."""
import math
import os
import random
import signal
import sys
import threading
import time
import urllib.request

import psycopg
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DB = dict(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], dbname=os.environ["DB_NAME"],
          user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], connect_timeout=3,
          application_name=os.environ["WORKER_ID"])
WEB_URL = os.environ["WEB_URL"]
LEASE = float(os.environ["LEASE_SECONDS"])
POLL = float(os.environ["POLL_INTERVAL_MS"]) / 1000
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0"))
WORKER_ID = os.environ["WORKER_ID"]
CLAIM_MODE = os.environ.get("CLAIM_MODE", "skip_locked")
STOP_GRACE = float(os.environ.get("STOP_GRACE_SECONDS", "25"))
CANCEL_PASSES = int(os.environ.get("CANCEL_PASSES", "4"))
HEARTBEAT = LEASE / 3

S = 256
Q_LO, Q_HI = 0.02, 0.98

TASKS = Counter("worker_tasks_total", "Задачі за результатом", ["result"])
CLAIMS = Counter("worker_claims_total", "Спроби захоплення за результатом", ["result"])
HEARTBEATS = Counter("worker_heartbeats_total", "Heartbeat за результатом", ["result"])
RETURNED = Counter("worker_tasks_returned_total", "Задачі, повернуті воркером у чергу під час зупинки")
STEP = Histogram("worker_task_duration_seconds", "Тривалість кроку задачі, с", ["step"],
                 buckets=[0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 1.024, 2.048])
IN_FLIGHT = Gauge("worker_tasks_in_flight", "Задач в обробці")

CLAIM = """
UPDATE tasks SET status = 'running', worker_id = %s,
       token = token + 1, attempts = attempts + 1,
       lease_until = now() + make_interval(secs => %s),
       started_at = now()
WHERE id = (SELECT id FROM tasks WHERE status = 'queued'
            ORDER BY created_at LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING id, record_id, seed, token
"""
CLAIM_FU = CLAIM.replace("FOR UPDATE SKIP LOCKED", "FOR UPDATE")
NAIVE_SELECT = "SELECT id FROM tasks WHERE status = 'queued' ORDER BY created_at LIMIT 1"
NAIVE_UPDATE = """
UPDATE tasks SET status = 'running', worker_id = %s,
       token = token + 1, attempts = attempts + 1,
       lease_until = now() + make_interval(secs => %s),
       started_at = now()
WHERE id = %s
RETURNING id, record_id, seed, token
"""
HEARTBEAT_SQL = ("UPDATE tasks SET lease_until = now() + make_interval(secs => %s) "
                 "WHERE id = %s AND token = %s")
RETURN_SQL = ("UPDATE tasks SET status = 'queued', worker_id = NULL "
              "WHERE id = %s AND token = %s AND status = 'running'")
CHECK_SQL = "SELECT cancel_requested FROM tasks WHERE id = %s AND token = %s"
CANCEL_SQL = ("UPDATE tasks SET status = 'cancelled', finished_at = clock_timestamp() "
              "WHERE id = %s AND token = %s AND status = 'running'")

current = None
cur_lock = threading.Lock()
stopping = threading.Event()
t_term = None


class Cancelled(Exception):
    pass


def return_current():
    with cur_lock:
        snap = current
    if snap is None:
        return
    n = 0
    try:
        with psycopg.connect(**DB, autocommit=True) as c:
            n = c.execute(RETURN_SQL, (snap[0], snap[1])).rowcount
    except Exception as e:
        print(f"{WORKER_ID}: не вдалося повернути задачу {snap[0]}: {e}", flush=True)
    if n:
        RETURNED.inc()
        print(f"RETURNED task={snap[0]} worker={WORKER_ID} token={snap[1]}", flush=True)
    print(f"STOP worker={WORKER_ID}: ліміт {STOP_GRACE:g} с на зупинку спливло, вихід з кодом 0", flush=True)
    os._exit(0)


def on_sigterm(signum, frame):
    global t_term
    if stopping.is_set():
        return
    t_term = time.time()
    stopping.set()
    timer = threading.Timer(STOP_GRACE, return_current)
    timer.daemon = True
    timer.start()


def heartbeat_loop():
    conn, warned = None, None
    while True:
        time.sleep(HEARTBEAT)
        with cur_lock:
            snap = current
        if snap is None:
            continue
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(**{**DB, "application_name": WORKER_ID + "-hb"}, autocommit=True)
            n = conn.execute(HEARTBEAT_SQL, (LEASE, snap[0], snap[1])).rowcount
        except Exception as e:
            print(f"{WORKER_ID}: heartbeat помилка: {e}", flush=True)
            conn = None
            continue
        if n:
            HEARTBEATS.labels("ok").inc()
        else:
            HEARTBEATS.labels("lost").inc()
            if warned != snap:
                warned = snap
                print(f"{WORKER_ID}: heartbeat задачі {snap[0]}, токен {snap[1]}: оновлено 0 рядків "
                      f"(задача вже не належить цьому воркеру)", flush=True)


def process(seed, check):
    n = S * S
    k = CANCEL_PASSES
    bounds = [n * i // k for i in range(k + 1)]
    x = seed
    pixels = []
    hist = [0] * 4096
    for b in range(k):
        for _ in range(bounds[b], bounds[b + 1]):
            x = (1103515245 * x + 12345) & 0x7FFFFFFF
            p = x >> 19
            pixels.append(p)
            hist[p] += 1
        check()

    lo = hi = None
    cum = 0
    for v in range(4096):
        cum += hist[v]
        if lo is None and cum >= Q_LO * n:
            lo = v
        if cum >= Q_HI * n:
            hi = v
            break
    check()

    span = (hi - lo) or 1                 
    removed = total = total2 = 0
    for b in range(k):
        for p in pixels[bounds[b]:bounds[b + 1]]:
            if p < lo or p > hi:
                removed += 1
            else:
                q = int(4095 * (p - lo) / span + 0.5)
                total += q
                total2 += q * q
        check()
    kept = n - removed
    mean = total / kept
    std = math.sqrt(max(total2 / kept - mean * mean, 0))
    return {"p_lo": lo, "p_hi": hi, "removed": removed,
            "mean": round(mean, 4), "std": round(std, 4)}


def make_check(conn, task):
    def check():
        row = conn.execute(CHECK_SQL, (task["id"], task["token"])).fetchone()
        if row and row["cancel_requested"]:
            raise Cancelled()
    return check


signal.signal(signal.SIGTERM, on_sigterm)
start_http_server(9100)
threading.Thread(target=heartbeat_loop, daemon=True).start()
print(f"{WORKER_ID}: старт, L={LEASE:g} с, heartbeat кожні {HEARTBEAT:.2f} с, "
      f"T_poll={POLL * 1000:g} мс, FAIL_RATE={FAIL_RATE}, захоплення: {CLAIM_MODE}, "
      f"ліміт на зупинку {STOP_GRACE:g} с, проходів скасування {CANCEL_PASSES}", flush=True)

while not stopping.is_set():
    try:
        with psycopg.connect(**DB, autocommit=True, row_factory=dict_row) as conn:
            while not stopping.is_set():
                t = time.perf_counter()
                if CLAIM_MODE == "naive":
                    row = conn.execute(NAIVE_SELECT).fetchone()
                    task = (conn.execute(NAIVE_UPDATE, (WORKER_ID, LEASE, row["id"])).fetchone()
                            if row else None)
                elif CLAIM_MODE == "for_update":
                    task = conn.execute(CLAIM_FU, (WORKER_ID, LEASE)).fetchone()
                else:
                    task = conn.execute(CLAIM, (WORKER_ID, LEASE)).fetchone()
                STEP.labels("claim").observe(time.perf_counter() - t)
                if task is None:
                    CLAIMS.labels("empty").inc()
                    stopping.wait(POLL)
                    continue
                CLAIMS.labels("task").inc()
                print(f"CLAIM task={task['id']} worker={WORKER_ID} token={task['token']}", flush=True)
                with cur_lock:
                    current = (task["id"], task["token"])
                IN_FLIGHT.inc()
                try:
                    t = time.perf_counter()
                    with urllib.request.urlopen(f"{WEB_URL}/lab-results/{task['record_id']}", timeout=5) as r:
                        r.read()
                    STEP.labels("fetch").observe(time.perf_counter() - t)

                    t = time.perf_counter()
                    result = process(task["seed"], make_check(conn, task))
                    STEP.labels("process").observe(time.perf_counter() - t)

                    if random.random() < FAIL_RATE:
                        os._exit(1)

                    t = time.perf_counter()
                    with conn.transaction():
                        done = conn.execute(
                            "UPDATE tasks SET status = 'done', finished_at = now() "
                            "WHERE id = %s AND token = %s",
                            (task["id"], task["token"])).rowcount
                        if done:
                            conn.execute(
                                "INSERT INTO task_results (task_id, token, worker_id, result) "
                                "VALUES (%s, %s, %s, %s) ON CONFLICT (task_id) DO NOTHING",
                                (task["id"], task["token"], WORKER_ID, Jsonb(result)))
                    STEP.labels("store").observe(time.perf_counter() - t)
                    if done:
                        TASKS.labels("done").inc()
                        print(f"DONE task={task['id']} worker={WORKER_ID}", flush=True)
                    else:
                        TASKS.labels("lost").inc()
                        print(f"{WORKER_ID}: запис результату задачі {task['id']}, токен {task['token']}: "
                              f"оновлено 0 рядків (задача вже не належить цьому воркеру)", flush=True)
                except psycopg.OperationalError:
                    raise
                except Cancelled:
                    conn.execute(CANCEL_SQL, (task["id"], task["token"]))
                    TASKS.labels("cancelled").inc()
                    print(f"CANCELLED task={task['id']} worker={WORKER_ID}", flush=True)
                except Exception as e:
                    TASKS.labels("failed").inc()
                    print(f"{WORKER_ID}: задача {task['id']} помилка: {e}", flush=True)
                finally:
                    IN_FLIGHT.dec()
                    with cur_lock:
                        current = None
    except psycopg.OperationalError as e:
        print(f"{WORKER_ID}: втрачено зʼєднання з базою ({e}), перепідключення", flush=True)
        stopping.wait(1)

dt = time.time() - t_term if t_term else 0.0
print(f"STOP worker={WORKER_ID}: SIGTERM отримано, поточну задачу завершено, вихід з кодом 0 "
      f"через {dt:.2f} с", flush=True)
sys.exit(0)