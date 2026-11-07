"""Крок 5: зупинка одного з двох воркерів на 2·L секунд під час обробки 100 задач. """
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
LEASE = 10
FREEZE_S = 2 * LEASE
PSQL = ["kubectl", "exec", "postgres-0", "--", "psql", "-U", "lab1", "-d", "labdb", "-At", "-c"]


def sh(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"Помилка команди {' '.join(cmd[:6])}: {r.stderr.strip()}")
    return r.stdout


def sql(q):
    return sh(PSQL + [q]).strip()


def task_row(tid):
    return sql(f"SELECT id, status, attempts, token, coalesce(worker_id, '-') FROM tasks WHERE id = {tid}")


def proc_state(pod):
    out = sh(["kubectl", "exec", pod, "--", "cat", "/proc/1/status"], check=False)
    m = re.search(r"State:\s+(\w)", out)
    return m.group(1) if m else "?"


def host_pid(node, pod):
    script = ('for d in /proc/[0-9]*; do p=${d#/proc/}; '
              'if tr "\\0" " " < $d/cmdline 2>/dev/null | grep -q "^python worker.py"; then '
              'if tr "\\0" "\\n" < $d/environ 2>/dev/null | grep -qx "HOSTNAME=%s"; then echo $p; fi; fi; done' % pod)
    out = sh(["docker", "exec", node, "sh", "-c", script], check=False).split()
    return out[0] if out else None


def freeze(pod):
    """Спершу команда із завдання (kill -STOP 1 у контейнері), далі запасний варіант із вузла."""
    sh(["kubectl", "exec", pod, "--", "python", "-c", "import os,signal;os.kill(1,signal.SIGSTOP)"], check=False)
    time.sleep(1)
    st = proc_state(pod)
    print(f"Спроба kubectl exec ... kill -STOP 1: стан процесу PID 1 = {st} (T – зупинено)")
    if st == "T":
        return ("kubectl exec ... kill -STOP 1",
                lambda: sh(["kubectl", "exec", pod, "--", "python", "-c",
                            "import os,signal;os.kill(1,signal.SIGCONT)"], check=False))
    node = sh(["kubectl", "get", "pod", pod, "-o", "jsonpath={.spec.nodeName}"]).strip()
    pid = host_pid(node, pod)
    if not pid:
        raise SystemExit("Не знайдено процес воркера на вузлі")
    sh(["docker", "exec", node, "sh", "-c", f"kill -STOP {pid}"])
    time.sleep(1)
    st = proc_state(pod)
    print(f"Запасний варіант: kill -STOP {pid} на вузлі {node}: стан PID 1 в контейнері = {st}")
    if st != "T":
        raise SystemExit("Не вдалося зупинити воркера")
    return (f"docker exec {node} kill -STOP {pid} (PID процесу на вузлі)",
            lambda: sh(["docker", "exec", node, "sh", "-c", f"kill -CONT {pid}"]))


def main():
    pods = sh(["kubectl", "get", "pods", "-l", "app=worker", "-o",
               "jsonpath={.items[*].metadata.name}"]).split()
    if len(pods) != 2:
        raise SystemExit("Потрібно рівно 2 воркери: kubectl scale deployment/worker --replicas=2")
    victim, other = pods
    print(f"Воркери: {victim} (буде зупинено) і {other}")
    sql("TRUNCATE tasks, task_results RESTART IDENTITY")
    gen = subprocess.Popen([sys.executable, os.path.join(ROOT, "loadgen", "taskload.py"),
                            "--url", "http://localhost:8080", "--rate", "100", "--count", "100",
                            "--label", "step5"])
    deadline = time.time() + 30
    while sql(f"SELECT count(*) FROM tasks WHERE status = 'running' AND worker_id = '{victim}'") == "0":
        if time.time() > deadline:
            gen.kill()
            raise SystemExit("Воркер не взяв жодної задачі")
        time.sleep(0.05)

    method, resume = freeze(victim)
    t_frozen = time.time()
    held = sql(f"SELECT id, attempts, token FROM tasks WHERE status = 'running' "
               f"AND worker_id = '{victim}'").splitlines()
    if not held:
        resume()
        gen.wait()
        raise SystemExit("На момент зупинки воркер не тримав задачі. Повтори запуск.")
    tid, att0, tok0 = held[0].split("|")
    print(f"\nЗупинено: {method}")
    print(f"Задача в обробці на момент зупинки: id={tid}, attempts={att0}, token={tok0}")
    while True:
        rem = FREEZE_S - (time.time() - t_frozen)
        if rem <= 0:
            break
        time.sleep(min(5, rem))
        print(f"  +{time.time() - t_frozen:4.1f} с: {task_row(tid)}")
    resume()
    print(f"Відновлено через {time.time() - t_frozen:.1f} с (2·L = {FREEZE_S} с)")
    gen.wait()
    deadline = time.time() + 120
    while sql("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running')") != "0":
        if time.time() > deadline:
            print("Черга не спорожніла за 120 с")
            break
        time.sleep(0.5)
    time.sleep(2)

    after = sql(f"SELECT status, attempts, token, worker_id, started_at, finished_at "
                f"FROM tasks WHERE id = {tid}")
    n_res = sql(f"SELECT count(*) FROM task_results WHERE task_id = {tid}")
    res_row = sql(f"SELECT worker_id, token FROM task_results WHERE task_id = {tid}")
    by_status = sql("SELECT status || '=' || count(*) FROM tasks GROUP BY status ORDER BY status").replace("\n", ", ")
    multi = sql("SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id "
                "HAVING count(*) > 1) t")
    n_all = sql("SELECT count(*) FROM task_results")
    att_gt1 = sql("SELECT coalesce(string_agg(id::text, ','), '-') FROM tasks WHERE attempts > 1")
    vlog = sh(["kubectl", "logs", victim, "--tail=-1"], check=False)
    lines0 = [l for l in vlog.splitlines() if "оновлено 0 рядків" in l]
    wlog = sh(["kubectl", "logs", "deploy/web", "--tail=-1"], check=False)
    reap = [l for l in wlog.splitlines() if l.startswith("reaper:") and f"задача {tid} " in l]

    print(f"\n=== Результат кроку 5 ===")
    print(f"Задача id={tid}: до зупинки attempts={att0}, token={tok0}")
    print(f"Після відновлення (status|attempts|token|воркер, що завершив|started_at|finished_at):\n  {after}")
    print(f"Воркер, який завершив задачу (з task_results): {res_row.replace('|', ', token=')}")
    print(f"Рядків у task_results для задачі {tid}: {n_res}")
    print("Журнал зупиненого воркера, рядки про 0 оновлених рядків:")
    for l in lines0 or ["  (немає)"]:
        print("  " + l)
    print("Журнал web (повернення в чергу):")
    for l in reap or ["  (немає)"]:
        print("  " + l)
    print(f"Усього задач за статусами: {by_status}")
    print(f"Усього рядків у task_results: {n_all}; задач із кількома результатами: {multi}")
    print(f"Задачі з attempts > 1: {att_gt1}")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "step5_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"victim": victim, "other": other, "method": method, "task_id": tid,
                   "attempts_before": att0, "token_before": tok0, "after": after,
                   "result_rows": n_res, "result_row": res_row, "status_counts": by_status,
                   "multi_results": multi, "victim_log_zero_rows": lines0, "reaper_log": reap},
                  f, ensure_ascii=False, indent=1)
    print("Збережено: results/tasks/step5_summary.json")


if __name__ == "__main__":
    main()