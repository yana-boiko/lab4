"""Крок 8: зупинка воркерів за SIGTERM (kubectl rollout restart) під навантаженням 0,7 · X(R)."""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

from step6_run import sh, sql, wait_ready

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
LOGS = os.path.join(OUT, "step8_logs")
R = 4
URL = "http://localhost:8080"


class PodWatch:
    """Стежить за подами воркерів і запамʼятовує коди завершення їхніх контейнерів."""

    def __init__(self):
        self.codes = {}
        self.proc = subprocess.Popen(["kubectl", "get", "pods", "-l", "app=worker", "-w", "-o", "json"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()

    def _handle(self, obj):
        items = obj.get("items") if obj.get("kind") == "List" else [obj]
        for pod in items or []:
            name = pod.get("metadata", {}).get("name")
            for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                term = (cs.get("state") or {}).get("terminated")
                if term:
                    self.codes[name] = (term.get("exitCode"), term.get("reason"))

    def _read(self):
        dec, buf = json.JSONDecoder(), ""
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            buf += data.decode("utf-8", "replace")
            while True:
                buf = buf.lstrip()
                if not buf:
                    break
                try:
                    obj, i = dec.raw_decode(buf)
                except ValueError:
                    break
                buf = buf[i:]
                self._handle(obj)

    def stop(self):
        self.proc.terminate()
        self.t.join(timeout=3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--restart-at", type=int, default=20)
    ap.add_argument("--backlog", type=int, default=0)
    a = ap.parse_args()

    xr = json.load(open(os.path.join(OUT, "step6_fit.json"), encoding="utf-8"))["X"][-1]
    rate = round(0.7 * xr, 2)
    print(f"=== Крок 8, {a.label}: інтенсивність 0,7 · X({R}) = {rate} задач/с, {a.duration} с, "
          f"перезапуск на {a.restart_at}-й секунді ===")
    pods = wait_ready(R)
    print(f"Поди до перезапуску: {', '.join(pods)}")
    os.makedirs(os.path.join(LOGS, a.label), exist_ok=True)
    sql("TRUNCATE tasks, task_results RESTART IDENTITY")
    time.sleep(8)

    watch = PodWatch()
    time.sleep(2)
    followers = []
    for p in pods:
        fh = open(os.path.join(LOGS, a.label, p + ".log"), "w", encoding="utf-8")
        followers.append((subprocess.Popen(["kubectl", "logs", "-f", p], stdout=fh,
                                           stderr=subprocess.STDOUT), fh))

    if a.backlog:
        sql("INSERT INTO tasks (record_id, seed) SELECT r.id, 1 + floor(random() * 2147483646)::bigint "
            "FROM generate_series(1, 30) g CROSS JOIN (SELECT id FROM lab_results ORDER BY id LIMIT 100) r "
            f"LIMIT {a.backlog}")
        gen = None
        print(f"У чергу вставлено {a.backlog} задач, усі воркери зайняті")
    else:
        gen = subprocess.Popen([sys.executable, os.path.join(ROOT, "loadgen", "taskload.py"), "--url", URL,
                                "--rate", str(rate), "--duration", str(a.duration), "--label", a.label])
    t0 = time.time()
    time.sleep(max(0, a.restart_at - (time.time() - t0)))
    print(f"\n>>> kubectl rollout restart deployment/worker (на {time.time() - t0:.1f} с прогону)")
    sh(["kubectl", "rollout", "restart", "deployment/worker"])
    if gen:
        gen.wait()
    sh(["kubectl", "rollout", "status", "deployment/worker", "--timeout=240s"])
    wait_ready(R)

    deadline = time.time() + 600
    while sql("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running')") != "0":
        if time.time() > deadline:
            print("УВАГА: черга не спорожніла за 600 с")
            break
        time.sleep(1)
    time.sleep(3)
    for pr, fh in followers:
        try:
            pr.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pr.terminate()
        fh.close()
    watch.stop()

    logs = {p: open(os.path.join(LOGS, a.label, p + ".log"), encoding="utf-8", errors="replace").read()
            for p in pods}
    allc = "\n".join(logs.values())
    returned = re.findall(r"RETURNED task=(\d+)", allc)
    stops = {p: len(re.findall(r"^STOP worker=", t, flags=re.M)) for p, t in logs.items()}

    n = int(sql("SELECT count(*) FROM tasks"))
    stat = sql("SELECT status || '=' || count(*) FROM tasks GROUP BY status ORDER BY status").replace("\n", ", ")
    failed = int(sql("SELECT count(*) FROM tasks WHERE status = 'failed'"))
    multi = int(sql("SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id "
                    "HAVING count(*) > 1) t"))
    att = int(sql("SELECT count(*) FROM tasks WHERE attempts > 1"))
    att_ids = sql("SELECT coalesce(string_agg(id::text, ',' ORDER BY id), '-') FROM tasks WHERE attempts > 1")
    n_res = int(sql("SELECT count(*) FROM task_results"))

    print("\n=== Результат кроку 8 ===")
    print("Коди завершення контейнерів старих подів (за спостереженням подів) і рядки STOP у їхніх журналах:")
    for p in pods:
        c = watch.codes.get(p)
        print(f"  {p}: код {c[0] if c else 'не зафіксовано'}"
              f"{' (' + str(c[1]) + ')' if c and c[1] else ''}; рядків STOP у журналі: {stops[p]}")
    print(f"Повернуто в чергу воркерами під час зупинки (рядки RETURNED): {len(returned)}"
          f"{' (задачі ' + ', '.join(returned) + ')' if returned else ''}")
    for tid in returned:
        row = sql(f"SELECT status || '|attempts=' || attempts || '|token=' || token || '|завершив=' || "
                  f"coalesce(worker_id, '-') FROM tasks WHERE id = {tid}")
        res = sql(f"SELECT count(*) FROM task_results WHERE task_id = {tid}")
        print(f"  задача {tid}: {row}; рядків у task_results: {res}")
    print(f"Усього задач: {n}; за статусами: {stat}")
    print(f"Задач у статусі failed: {failed}")
    print(f"Задач з більше ніж одним результатом: {multi}; рядків у task_results: {n_res}")
    print(f"Задач з attempts > 1: {att} (id: {att_ids})")

    summary = {"label": a.label, "rate": rate, "pods": pods,
               "exit_codes": {p: watch.codes.get(p) for p in pods}, "stop_lines": stops,
               "returned": returned, "n": n, "status": stat, "failed": failed, "multi_results": multi,
               "attempts_gt1": att, "attempts_gt1_ids": att_ids}
    with open(os.path.join(OUT, f"{a.label}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"\nЗбережено: results/tasks/{a.label}.json і results/tasks/step8_logs/{a.label}/")


if __name__ == "__main__":
    main()