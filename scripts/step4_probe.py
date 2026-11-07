"""Крок 4: серія «створити задачі, дочекатися порожньої черги» з вимірюваннями."""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
PSQL = ["kubectl", "exec", "postgres-0", "--", "psql", "-U", "lab1", "-d", "labdb", "-At", "-c"]
INF = float("inf")
LINE = re.compile(r"^(\w+)\{([^}]*)\}\s+(\S+)")


def sh(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"Помилка команди {' '.join(cmd[:5])}: {r.stderr.strip()}")
    return r.stdout


def sql(q):
    return sh(PSQL + [q]).strip()


def read_pod(pod):
    code = "import urllib.request as u;print(u.urlopen('http://127.0.0.1:9100/metrics').read().decode())"
    return sh(["kubectl", "exec", pod, "--", "python", "-c", code])


def parse(text):
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            yield m[1], dict(re.findall(r'(\w+)="([^"]*)"', m[2])), float(m[3])


def snapshot():
    names = sh(["kubectl", "get", "pods", "-l", "app=worker", "-o",
                "jsonpath={.items[*].metadata.name}"]).split()
    if not names:
        raise SystemExit("Немає подів воркера")
    with ThreadPoolExecutor(len(names)) as ex:
        texts = list(ex.map(read_pod, names))
    s = {"t": time.time(), "pods": len(names), "task": 0.0, "empty": 0.0, "claim_buckets": {}}
    for txt in texts:
        for name, lab, val in parse(txt):
            if name == "worker_claims_total" and lab.get("result") in ("task", "empty"):
                s[lab["result"]] += val
            elif name == "worker_task_duration_seconds_bucket" and lab.get("step") == "claim":
                s["claim_buckets"][lab["le"]] = s["claim_buckets"].get(lab["le"], 0.0) + val
    return s


def snapshot_retry():
    for _ in range(30):
        try:
            return snapshot()
        except SystemExit:
            time.sleep(1)
    raise SystemExit("Метрики воркерів недоступні")


def p99(delta):
    pts = sorted(((INF if k == "+Inf" else float(k)), v) for k, v in delta.items())
    n = pts[-1][1]
    if n <= 0:
        return None
    r = 0.99 * n
    pb = pc = 0.0
    for b, c in pts:
        if r <= c:
            if b == INF:
                return (None, pb, INF, n)
            return (pb + (b - pb) * (r - pc) / (c - pc), pb, b, n)
        pb, pc = b, c
    return None


def lock_sampler(stop, out):
    q = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
    t0, k = time.time(), 0
    while not stop.is_set():
        d = t0 + k - time.time()
        if d > 0 and stop.wait(d):
            break
        k += 1
        try:
            out.append((round(time.time() - t0, 1), int(sql(q))))
        except BaseException:
            pass


def duplicates():
    n = int(sql("SELECT count(*) FROM tasks"))
    res = int(sql("SELECT count(*) FROM task_results"))
    dup_tab = int(sql("SELECT count(*) FROM (SELECT task_id FROM task_results "
                      "GROUP BY task_id HAVING count(*) > 1) t"))
    att = int(sql("SELECT count(*) FROM tasks WHERE attempts > 1"))
    logs = sh(["kubectl", "logs", "-l", "app=worker", "--tail=-1"])
    cl = Counter(re.findall(r"CLAIM task=(\d+)", logs))
    dn = Counter(re.findall(r"DONE task=(\d+)", logs))
    return {"n": n, "results": res, "dup_table": dup_tab, "attempts_gt1": att,
            "claims_in_logs": sum(cl.values()), "dup_claims_logs": sum(1 for v in cl.values() if v > 1),
            "dup_done_logs": sum(1 for v in dn.values() if v > 1),
            "worker_errors_in_logs": len(re.findall("помилка", logs))}


def run(a):
    logs = sh(["kubectl", "logs", "-l", "app=worker", "--tail=-1"])
    if "CLAIM task=" in logs:
        raise SystemExit("У журналах воркерів уже є записи минулої серії. Перезапусти воркерів: "
                         "kubectl rollout restart deployment/worker")
    sql("TRUNCATE tasks, task_results RESTART IDENTITY")
    stop, samples = threading.Event(), []
    th = threading.Thread(target=lock_sampler, args=(stop, samples), daemon=True)
    th.start()
    A = snapshot_retry()
    t_start = time.time()
    gen = subprocess.run([sys.executable, os.path.join(ROOT, "loadgen", "taskload.py"), "--url", a.url,
                          "--rate", str(a.rate), "--count", str(a.count), "--label", a.label])
    if gen.returncode != 0:
        raise SystemExit("Генератор завершився з помилкою")
    deadline = time.time() + 300
    while True:
        busy, total = sql("SELECT count(*) FILTER (WHERE status IN ('queued','running')), "
                          "count(*) FROM tasks").split("|")
        if int(busy) == 0 and int(total) >= a.count:
            break
        if time.time() > deadline:
            raise SystemExit("Черга не спорожніла за 300 с")
        time.sleep(0.2)
    t_end = time.time()
    B = snapshot_retry()
    stop.set()
    th.join()

    delta = {k: B["claim_buckets"][k] - A["claim_buckets"].get(k, 0.0) for k in B["claim_buckets"]}
    est = p99(delta)
    dups = duplicates()
    max_lock = max((v for _, v in samples), default=None)
    print(f"\n=== Серія {a.label} ===")
    print(f"Воркерів: {B['pods']}; від початку генератора до порожньої черги: {t_end - t_start:.1f} с")
    print(f"Захоплень задач: {B['task'] - A['task']:.0f}; порожніх захоплень: {B['empty'] - A['empty']:.0f}")
    if est is None:
        print("p99 часу захоплення: немає даних")
    elif est[0] is None:
        print(f"p99 часу захоплення: > {est[1] * 1000:g} мс")
    else:
        print(f"p99 часу захоплення: {est[0] * 1000:.2f} мс (бакет ({est[1] * 1000:g}; {est[2] * 1000:g}] мс, "
              f"похибка до {(est[2] - est[1]) * 1000:g} мс, спостережень {est[3]:.0f})")
    print(f"Найбільша кількість рядків pg_stat_activity з wait_event_type = 'Lock': {max_lock} "
          f"(замірів: {len(samples)}, кожну секунду)")
    print(f"Задач n = {dups['n']}, рядків у task_results = {dups['results']}")
    print(f"Задач із кількома результатами (за таблицею): {dups['dup_table']}, D = {dups['dup_table'] / dups['n']:.4f}")
    print(f"Задач, захоплених більше ніж раз (за журналами): {dups['dup_claims_logs']}, "
          f"D = {dups['dup_claims_logs'] / dups['n']:.4f}")
    print(f"Задач, виконаних більше ніж раз (DONE у журналах): {dups['dup_done_logs']}")
    print(f"Задач з attempts > 1: {dups['attempts_gt1']}; рядків із помилкою в журналах воркерів: "
          f"{dups['worker_errors_in_logs']}")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{a.label}_locks.csv"), "w", encoding="utf-8") as f:
        f.write("t_s,lock_waiters\n" + "\n".join(f"{t},{v}" for t, v in samples) + "\n")
    with open(os.path.join(OUT, f"{a.label}_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"label": a.label, "A": A, "B": B, "p99": est, "max_lock": max_lock,
                   "duration_s": t_end - t_start, **dups}, f, ensure_ascii=False, indent=1)
    print(f"Збережено: results/tasks/{a.label}_summary.json і {a.label}_locks.csv")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--count", type=int, default=300)
    r.add_argument("--rate", type=int, default=100)
    r.add_argument("--url", default="http://localhost:8080")
    r.set_defaults(func=run)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()