"""Крок 6: прогін з непорожньою чергою для W воркерів."""
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import time
import math
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
N_TASKS = 400
WINDOW = 30
PSQL = ["kubectl", "exec", "postgres-0", "--", "psql", "-U", "lab1", "-d", "labdb", "-At"]
INF = float("inf")
LINE = re.compile(r"^(\w+)\{([^}]*)\}\s+(\S+)")
SAMPLE = ("if [ -f /sys/fs/cgroup/cpu.stat ]; then grep usage_usec /sys/fs/cgroup/cpu.stat | cut -d' ' -f2; "
          "else echo $(( $(cat /sys/fs/cgroup/cpuacct/cpuacct.usage) / 1000 )); fi; "
          "date +%s.%N; "
          "psql -U lab1 -d labdb -At -c \"SELECT xact_commit + xact_rollback FROM pg_stat_database "
          "WHERE datname = 'labdb'\"")
INSERT = ("INSERT INTO tasks (record_id, seed) "
          "SELECT r.id, 1 + floor(random() * 2147483646)::bigint "
          "FROM generate_series(1, 4) g CROSS JOIN (SELECT id FROM lab_results ORDER BY id LIMIT 100) r "
          f"LIMIT {N_TASKS}")


def sh(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"Помилка команди {' '.join(cmd[:6])}: {r.stderr.strip()}")
    return r.stdout


def sql(q, sep=None):
    return sh(PSQL + (["-F", sep] if sep else []) + ["-c", q]).strip()


def pods_state():
    items = json.loads(sh(["kubectl", "get", "pods", "-l", "app=worker", "-o", "json"]))["items"]
    live = [p for p in items if not p["metadata"].get("deletionTimestamp")]
    ready = [p["metadata"]["name"] for p in live if p["status"].get("phase") == "Running"
             and p["status"].get("containerStatuses")
             and all(c.get("ready") for c in p["status"]["containerStatuses"])]
    return len(items), ready


def wait_ready(w):
    deadline = time.time() + 240
    while True:
        total, ready = pods_state()
        if total == w and len(ready) == w:
            return sorted(ready)
        if time.time() > deadline:
            raise SystemExit(f"Поди воркерів не стали готовими: усього {total}, готових {len(ready)}")
        time.sleep(2)


def db_sample():
    out = sh(["kubectl", "exec", "postgres-0", "--", "sh", "-c", SAMPLE]).split()
    return {"cpu_s": float(out[0]) / 1e6, "t": float(out[1]), "xact": float(out[2])}


def read_pod(pod):
    code = "import urllib.request as u;print(u.urlopen('http://127.0.0.1:9100/metrics').read().decode())"
    return sh(["kubectl", "exec", pod, "--", "python", "-c", code])


def snapshot(pods):
    with ThreadPoolExecutor(len(pods)) as ex:
        texts = list(ex.map(read_pod, pods))
    s = {"claim_buckets": {}, "sum": {}, "count": {}}
    for txt in texts:
        for line in txt.splitlines():
            m = LINE.match(line)
            if not m:
                continue
            name, lab, val = m[1], dict(re.findall(r'(\w+)="([^"]*)"', m[2])), float(m[3])
            if name == "worker_task_duration_seconds_bucket" and lab.get("step") == "claim":
                s["claim_buckets"][lab["le"]] = s["claim_buckets"].get(lab["le"], 0.0) + val
            elif name == "worker_task_duration_seconds_sum":
                s["sum"][lab["step"]] = s["sum"].get(lab["step"], 0.0) + val
            elif name == "worker_task_duration_seconds_count":
                s["count"][lab["step"]] = s["count"].get(lab["step"], 0.0) + val
    return s


def p99(delta):
    pts = sorted(((INF if k == "+Inf" else float(k)), v) for k, v in delta.items())
    n = pts[-1][1]
    if n <= 0:
        return None
    r = 0.99 * n
    pb = pc = 0.0
    for b, c in pts:
        if r <= c and c > pc:
            if b == INF:
                return (None, pb, INF, n)
            return (pb + (b - pb) * (r - pc) / (c - pc), pb, b, n)
        pb, pc = b, c
    return None


def main():
    w = int(sys.argv[1])
    print(f"=== Прогін W = {w} ===")
    sh(["kubectl", "scale", "deployment/worker", f"--replicas={w}"])
    pods = wait_ready(w)
    print(f"Воркерів: {len(pods)}: {', '.join(pods)}")
    sql("TRUNCATE tasks, task_results RESTART IDENTITY")
    time.sleep(3)
    A, D0 = snapshot(pods), db_sample()
    sql(INSERT)
    print(f"У чергу вставлено {sql('SELECT count(*) FROM tasks')} задач")
    deadline = time.time() + 900
    while sql("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running')") != "0":
        if time.time() > deadline:
            raise SystemExit("Черга не спорожніла за 900 с")
        time.sleep(0.4)
    D1, B = db_sample(), snapshot(pods)

    rows = [r.split(",") for r in sql(
        "SELECT extract(epoch from started_at)::float8, extract(epoch from finished_at)::float8 "
        "FROM tasks WHERE status = 'done'", sep=",").splitlines()]
    starts = [float(r[0]) for r in rows]
    fins = [float(r[1]) for r in rows]
    t_end, t_begin = max(starts), min(starts)
    n = int(min(WINDOW, t_end - t_begin))
    w0 = t_end - n
    bins = [0] * n
    for f in fins:
        k = math.floor(f - w0)
        if 0 <= k < n:
            bins[k] += 1
    X = sum(bins) / n
    sd = statistics.stdev(bins)

    d = {k: B["claim_buckets"][k] - A["claim_buckets"].get(k, 0.0) for k in B["claim_buckets"]}
    est = p99(d)
    avg = {st: (B["sum"][st] - A["sum"].get(st, 0.0)) / (B["count"][st] - A["count"].get(st, 0.0))
           for st in ("claim", "fetch", "process", "store")}
    dt = D1["t"] - D0["t"]
    tps = (D1["xact"] - D0["xact"]) / dt
    cpu = (D1["cpu_s"] - D0["cpu_s"]) / dt
    stat = sql("SELECT status || '=' || count(*) FROM tasks GROUP BY status ORDER BY status").replace("\n", ", ")
    att = sql("SELECT count(*) FROM tasks WHERE attempts > 1")
    dup = sql("SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id HAVING count(*) > 1) t")

    print(f"\nЗадачі за статусами: {stat}; з attempts > 1: {att}; з кількома результатами: {dup}")
    print(f"Від першого до останнього захоплення: {t_end - t_begin:.1f} с; вікно вимірювання: {n} с"
          + (" (прогін коротший за 30 с, взято весь інтервал)" if n < WINDOW else ""))
    print(f"X({w}) = {X:.2f} задач/с; розкид (стандартне відхилення посекундних значень) = {sd:.2f}")
    if est is None:
        print("p99 часу захоплення: немає даних")
    elif est[0] is None:
        print(f"p99 часу захоплення: > {est[1] * 1000:g} мс")
    else:
        print(f"p99 часу захоплення: {est[0] * 1000:.2f} мс (бакет ({est[1] * 1000:g}; {est[2] * 1000:g}] мс, "
              f"похибка до {(est[2] - est[1]) * 1000:g} мс, спостережень {est[3]:.0f})")
    print(f"Транзакцій бази за секунду: {tps:.0f}")
    print(f"CPU пода бази: {cpu:.2f} ядра ({cpu * 100:.0f} % одного ядра)")
    print("Середня тривалість кроків, мс: " + ", ".join(f"{k} {v * 1000:.2f}" for k, v in avg.items()))

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"step6_W{w}_seconds.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["second_in_window", "completed"])
        for i, c in enumerate(bins):
            wr.writerow([i, c])
    with open(os.path.join(OUT, f"step6_W{w}.json"), "w", encoding="utf-8") as f:
        json.dump({"W": w, "X": X, "sd": sd, "window_s": n, "bins": bins,
                   "p99_claim_ms": None if not est or est[0] is None else est[0] * 1000,
                   "p99_bucket_ms": None if not est else [est[1] * 1000, est[2] * 1000],
                   "tps": tps, "db_cpu_cores": cpu, "avg_ms": {k: v * 1000 for k, v in avg.items()},
                   "status": stat, "attempts_gt1": int(att), "multi_results": int(dup),
                   "span_s": t_end - t_begin}, f, ensure_ascii=False, indent=1)
    print(f"Збережено: results/tasks/step6_W{w}.json і step6_W{w}_seconds.csv")


if __name__ == "__main__":
    main()