"""Крок 10 (Д6): скасування задач. Запуск: python scripts\\step10_run.py
Перед запуском: 4 воркери з новим кодом і FAIL_RATE = 0.
"""
import csv
import http.client
import json
import os
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from step6_run import sh, sql, wait_ready

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
R, BACKLOG, NEED = 4, 800, 20
_conn = [None]


def api(method, path):
    for _ in range(2):
        if _conn[0] is None:
            _conn[0] = http.client.HTTPConnection("127.0.0.1", 8080, timeout=10)
        try:
            _conn[0].request(method, path)
            r = _conn[0].getresponse()
            body = r.read()
            return r.status, (json.loads(body) if body else None)
        except Exception:
            _conn[0].close()
            _conn[0] = None
    raise SystemExit("API недоступний")


def rows(q):
    return [r.split("|") for r in sql(q).splitlines() if r]


def main():
    fr = sh(["kubectl", "exec", "deploy/worker", "--", "printenv", "FAIL_RATE"], check=False).strip()
    print(f"=== Крок 10 (Д6): FAIL_RATE воркерів = {fr}, воркерів {R}, у черзі {BACKLOG} задач ===")
    if fr != "0":
        raise SystemExit("Спершу поверни FAIL_RATE = 0: kubectl set env deployment/worker FAIL_RATE-")
    wait_ready(R)
    sql("TRUNCATE tasks, task_results RESTART IDENTITY")
    time.sleep(3)
    sql("INSERT INTO tasks (record_id, seed) SELECT r.id, 1 + floor(random() * 2147483646)::bigint "
        "FROM generate_series(1, 10) g CROSS JOIN (SELECT id FROM lab_results ORDER BY id LIMIT 100) r "
        f"LIMIT {BACKLOG}")
    last = int(sql("SELECT max(id) FROM tasks"))

    print("\n--- 1. Скасування задач у черзі (queued -> cancelled) ---")
    for tid in (last, last - 1, last - 2):
        st, b = api("DELETE", f"/tasks/{tid}")
        print(f"DELETE /tasks/{tid} -> {st}, status = {b['status']}, cancel_requested = {b['cancel_requested']}")
    st, b = api("DELETE", f"/tasks/{last}")
    print(f"повторний DELETE /tasks/{last} -> {st}, status = {b['status']}")
    queued_cancelled = [last, last - 1, last - 2]

    print("\n--- 2. Скасування задач у running (прапорець, воркер зупиняється сам) ---")
    codes, flagged, stopped, t_start = {}, 0, 0, time.time()
    while stopped < NEED:
        if time.time() - t_start > 180:
            raise SystemExit("Не вдалося зупинити 20 задач за 180 с")
        st, data = api("GET", "/tasks?status=running&limit=100")
        cand = [t for t in data["items"] if not t["cancel_requested"]]
        if not cand:
            continue
        t = max(cand, key=lambda x: x["started_at"])
        st2, _ = api("DELETE", f"/tasks/{t['id']}")
        codes[st2] = codes.get(st2, 0) + 1
        if st2 == 202:
            flagged += 1
            if flagged % 5 == 0:
                time.sleep(0.3)
                _, d3 = api("GET", "/tasks?status=cancelled&limit=100")
                stopped = sum(1 for x in d3["items"] if x["cancel_requested"])
    print("Відповіді DELETE для задач у running: " + ", ".join(f"{c} × {n}" for c, n in sorted(codes.items()))
          + " (202 – прапорець виставлено, 409 – задача встигла завершитись до запиту)")

    deadline = time.time() + 300
    while sql("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running')") != "0":
        if time.time() > deadline:
            print("УВАГА: черга не спорожніла за 300 с")
            break
        time.sleep(1)

    print("\n--- 3. Інші коди відповіді ---")
    done_id = int(sql("SELECT id FROM tasks WHERE status = 'done' ORDER BY id LIMIT 1"))
    st, b = api("DELETE", f"/tasks/{done_id}")
    print(f"DELETE /tasks/{done_id} (задача done) -> {st} {b}")
    st, b = api("DELETE", "/tasks/99999999")
    print(f"DELETE /tasks/99999999 (немає такої) -> {st} {b}")

    data = rows("SELECT id, status, attempts, extract(epoch from (finished_at - cancel_requested_at)) * 1000, "
                "(SELECT count(*) FROM task_results r WHERE r.task_id = t.id) "
                "FROM tasks t WHERE cancel_requested ORDER BY cancel_requested_at")
    canc = [(int(i), float(l), int(c)) for i, s, a, l, c in data if s == "cancelled"][:NEED]
    late = [int(i) for i, s, a, l, c in data if s == "done"]
    lat = np.array([x[1] for x in canc])
    res_rows = sum(x[2] for x in canc)
    qc_results = int(sql("SELECT count(*) FROM task_results WHERE task_id IN (%s)"
                         % ",".join(map(str, queued_cancelled))))
    print(f"\n=== Результат: час від запиту скасування до зупинки обробки ({len(canc)} задач у running) ===")
    print("id задачі | час, мс")
    for tid, l, c in canc:
        print(f"{tid:>9} | {l:7.2f}")
    print(f"p50 = {np.percentile(lat, 50):.2f} мс; p99 = {np.percentile(lat, 99):.2f} мс; "
          f"min = {lat.min():.2f} мс; max = {lat.max():.2f} мс; середнє = {lat.mean():.2f} мс")
    print(f"Статус цих задач: cancelled × {len(canc)}; рядків у task_results для них: {res_rows}")
    print(f"Скасування надійшло надто пізно (задача встигла стати done): {len(late)} (id: {late or '-'})")
    print(f"Скасовані в черзі {queued_cancelled}: рядків у task_results: {qc_results}")
    stat = sql("SELECT status || '=' || count(*) FROM tasks GROUP BY status ORDER BY status").replace("\n", ", ")
    print(f"Усього задач за статусами: {stat}; з attempts > 1: {sql('SELECT count(*) FROM tasks WHERE attempts > 1')}")

    json.dump({"p50_ms": float(np.percentile(lat, 50)), "p99_ms": float(np.percentile(lat, 99)),
               "min_ms": float(lat.min()), "max_ms": float(lat.max()), "mean_ms": float(lat.mean()),
               "n": len(canc), "result_rows": res_rows, "late": late, "delete_codes": codes, "status": stat},
              open(os.path.join(OUT, "step10_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "step10_latency.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task_id", "latency_ms", "result_rows"])
        w.writerows(canc)
    plt.figure(figsize=(9, 4.5))
    plt.bar(range(1, len(lat) + 1), lat, color="tab:blue")
    plt.axhline(np.percentile(lat, 50), color="green", linestyle="--", label=f"p50 = {np.percentile(lat, 50):.1f} мс")
    plt.axhline(np.percentile(lat, 99), color="red", linestyle="--", label=f"p99 = {np.percentile(lat, 99):.1f} мс")
    plt.xlabel("скасована задача (за порядком запитів)")
    plt.ylabel("від запиту скасування до зупинки, мс")
    plt.title("Час зупинки обробки після скасування (20 задач у running)")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "step10_latency.png"), dpi=150)
    print("\nЗбережено: results/tasks/step10_summary.json, step10_latency.csv, step10_latency.png")


if __name__ == "__main__":
    main()