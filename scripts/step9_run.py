"""Крок 9: FAIL_RATE = 0,15, прогін 0,7 · X(R) протягом 300 с, у середині – сценарій Ф2 (pg_terminate_backend)."""
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from step6_run import PSQL, sh, sql, wait_ready

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
R, DURATION, KILL_AT, TAIL, F, MAX_ATTEMPTS = 4, 300, 150, 20, 0.15, 3
URL, PROM = "http://localhost:8080", "http://localhost:9090"

KILL_DO = """
DO $$
DECLARE r record; i int := 0;
BEGIN
  LOOP
    PERFORM pg_stat_clear_snapshot();
    SELECT t.id, t.worker_id, t.token, t.attempts, a.pid INTO r
    FROM tasks t JOIN pg_stat_activity a ON a.application_name = t.worker_id
    WHERE t.status = 'running' AND t.attempts = 1 AND a.pid <> pg_backend_pid()
    ORDER BY random() LIMIT 1;
    IF FOUND THEN
      PERFORM pg_terminate_backend(r.pid);
      RAISE NOTICE 'KILLED|%|%|%|%|%', r.id, r.worker_id, r.token, r.attempts, extract(epoch from clock_timestamp());
      RETURN;
    END IF;
    i := i + 1;
    IF i > 3000 THEN RAISE NOTICE 'NOT_KILLED'; RETURN; END IF;
    PERFORM pg_sleep(0.05);
  END LOOP;
END $$;
"""


def rows(q):
    return [r.split("|") for r in sql(q).splitlines() if r]


def pods_summary():
    items = json.loads(sh(["kubectl", "get", "pods", "-l", "app=worker", "-o", "json"]))["items"]
    out = []
    for p in sorted(items, key=lambda x: x["metadata"]["name"]):
        cs = (p["status"].get("containerStatuses") or [{}])[0]
        st = cs.get("state") or {}
        state = "Running" if "running" in st else ((st.get("waiting") or {}).get("reason") or "Terminated")
        out.append((p["metadata"]["name"], cs.get("restartCount", 0), state))
    return out


def prom_range(q, a, b, step=5):
    url = PROM + "/api/v1/query_range?" + urllib.parse.urlencode({"query": q, "start": a, "end": b, "step": step})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            res = json.load(r)["data"]["result"]
        if not res:
            return None
        return [float(t) for t, _ in res[0]["values"]], [float(v) for _, v in res[0]["values"]]
    except Exception as e:
        print(f"  (Prometheus недоступний: {e})")
        return None

def reconnect_lines(text, tid, token):
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if re.search(rf"CLAIM task={tid} .*token={token}\b", l)]
    if not idx:
        return []
    return [l for l in lines[idx[-1]:] if "перепідключення" in l]

def main():
    xr = json.load(open(os.path.join(OUT, "step6_fit.json"), encoding="utf-8"))["X"][-1]
    rate = round(0.7 * xr, 2)
    print(f"=== Крок 9: FAIL_RATE = {F}, інтенсивність 0,7 · X({R}) = {rate} задач/с, {DURATION} с, "
          f"сценарій Ф2 не раніше {KILL_AT}-ї секунди ===")
    env = sh(["kubectl", "get", "deploy", "worker", "-o",
              "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='FAIL_RATE')].value}"]).strip()
    print(f"FAIL_RATE у Deployment worker: {env or 'не задано'}")
    if env != "0.15":
        raise SystemExit("Спершу виконай: kubectl set env deployment/worker FAIL_RATE=0.15")
    pods = wait_ready(R)
    print(f"Поди: {', '.join(pods)}")
    sql("TRUNCATE tasks, task_results RESTART IDENTITY")
    time.sleep(8)

    gen = subprocess.Popen([sys.executable, os.path.join(ROOT, "loadgen", "taskload.py"), "--url", URL,
                            "--rate", str(rate), "--duration", str(DURATION), "--label", "step9"])
    t0 = time.time()
    killed, next_report = None, 30
    while gen.poll() is None:
        el = time.time() - t0
        if killed is None and el >= KILL_AT:
            print(f"\n>>> Сценарій Ф2 на {el:.0f}-й секунді: очікування воркера із задачею в обробці і завершення його сесії бази")
            p = subprocess.run(PSQL + ["-c", KILL_DO], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            m = re.search(r"KILLED\|(\d+)\|([^|]+)\|(\d+)\|(\d+)\|([\d.]+)", p.stdout + p.stderr)
            if m:
                killed = {"id": int(m[1]), "pod": m[2], "token": int(m[3]), "attempts": int(m[4]),
                          "t_kill": float(m[5]), "run_s": time.time() - t0}
                print(f"Завершено сесію бази воркера {killed['pod']} під час виконання задачі {killed['id']} "
                      f"(attempts = {killed['attempts']}, token = {killed['token']}) на {killed['run_s']:.0f}-й секунді прогону")

                text = ""
                for _ in range(15):
                    time.sleep(1)
                    prev = sh(["kubectl", "logs", killed["pod"], "--previous"], check=False)
                    cur = sh(["kubectl", "logs", killed["pod"]], check=False)
                    text = prev + "\n" + cur
                    if reconnect_lines(text, killed["id"], killed["token"]):
                        break
                os.makedirs(os.path.join(OUT, "step9_logs"), exist_ok=True)
                open(os.path.join(OUT, "step9_logs", killed["pod"] + ".log"), "w", encoding="utf-8").write(text)
                killed["reconnect"] = reconnect_lines(text, killed["id"], killed["token"])
                killed["claims"] = [l for l in text.splitlines() if re.search(rf"task={killed['id']}\b", l)]
            else:
                print("Не вдалося знайти воркера із задачею в обробці: " + (p.stdout + p.stderr).strip())
                killed = {}
            continue
        if el >= next_report:
            print(f"  +{el:3.0f} с: " + "; ".join(f"{n[-5:]}: перезапусків {r}, {s}" for n, r, s in pods_summary()))
            next_report += 30
        time.sleep(0.5)
    print(f"\nСтворення задач завершено; ще {TAIL} с збір даних (черга не спорожнюється: воркери в CrashLoopBackOff)")
    time.sleep(TAIL)
    ps = pods_summary()
    print("kubectl get pods -l app=worker:")
    print(sh(["kubectl", "get", "pods", "-l", "app=worker"]).rstrip())

    n = int(sql("SELECT count(*) FROM tasks"))
    t_first = float(sql("SELECT min(extract(epoch from created_at)) FROM tasks"))
    stat = sql("SELECT status || '=' || count(*) FROM tasks GROUP BY status ORDER BY status").replace("\n", ", ")
    all_att = {int(a): int(c) for a, c in rows("SELECT attempts, count(*) FROM tasks GROUP BY attempts")}
    res_att, failed = {}, 0
    for a, s, c in rows("SELECT attempts, status, count(*) FROM tasks WHERE status IN ('done', 'failed') "
                        "GROUP BY attempts, status"):
        res_att[int(a)] = res_att.get(int(a), 0) + int(c)
        if s == "failed":
            failed += int(c)
    resolved = sum(res_att.values())
    gt1 = sum(c for a, c in res_att.items() if a > 1)
    multi = int(sql("SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id "
                    "HAVING count(*) > 1) t"))
    n_res = int(sql("SELECT count(*) FROM task_results"))
    done_t = [float(x) for x in sql("SELECT extract(epoch from finished_at) FROM tasks WHERE status = 'done'").split()]
    bins = np.zeros(DURATION + TAIL + 1)
    for t in done_t:
        k = math.floor(t - t_first)
        if 0 <= k < len(bins):
            bins[k] += 1
    x_all = bins[:DURATION].sum() / DURATION
    x_last = bins[DURATION - 120:DURATION].sum() / 120

    print("\n=== Результат кроку 9 ===")
    total_restarts = sum(r for _, r, _ in ps)
    print(f"Перезапусків контейнерів worker за прогін (RESTARTS): {', '.join(str(r) for _, r, _ in ps)}; "
          f"разом {total_restarts}; стани: {', '.join(s for _, _, s in ps)}")
    print(f"Задач створено: {n}; за статусами: {stat}")
    print("\nРозподіл за attempts (усі задачі | завершені done/failed | частка серед завершених | за моделлю f = 0,15):")
    theory = {1: 1 - F, 2: F * (1 - F), 3: F ** 2}
    for a in sorted(set(all_att) | set(res_att)):
        th = f"{100 * theory[a]:.2f} %" if a in theory else "-"
        sh_res = f"{100 * res_att.get(a, 0) / resolved:.2f} %" if resolved else "-"
        print(f"  attempts = {a}: {all_att.get(a, 0)} | {res_att.get(a, 0)} | {sh_res} | {th}")
    p1 = gt1 / resolved
    p3 = failed / resolved
    ci = 1.96 * math.sqrt(F * (1 - F) / resolved)
    print(f"Завершених задач: {resolved}")
    print(f"Частка attempts > 1 серед завершених: {gt1}/{resolved} = {100 * p1:.2f} % (f = 15 %, "
          f"очікуваний розкид ±{100 * ci:.1f} п.п.)")
    print(f"Частка failed серед завершених: {failed}/{resolved} = {100 * p3:.2f} % (f³ = {100 * F ** 3:.2f} %)")
    print(f"Задач з більше ніж одним результатом: {multi}; рядків у task_results: {n_res}")
    print(f"\nX у прогоні (done за секунду): за {DURATION} с {x_all:.2f}; за останні 120 с {x_last:.2f} "
          f"(λ = {rate}; X({R}) без падінь зі кроку 6 = {xr:.2f})")
    print("Завершено задач за секунду по 30-секундних відрізках: " +
          ", ".join(f"{bins[i:i + 30].mean():.1f}" for i in range(0, DURATION, 30)))

    summary = {"rate": rate, "n": n, "status": stat, "restarts": {p: r for p, r, _ in ps},
               "all_attempts": all_att, "resolved_attempts": res_att, "failed": failed,
               "resolved": resolved, "multi_results": multi, "X_run": x_all, "X_last120": x_last}
    t_kill_rel = None
    if killed:
        tid = killed["id"]
        print(f"\nОчікування, поки задача {tid} буде завершена (до 10 хвилин: воркери в CrashLoopBackOff оживають рідко)...")
        t_wait = time.time()
        while time.time() - t_wait < 600:
            if sql(f"SELECT status FROM tasks WHERE id = {tid}") in ("done", "failed", "cancelled"):
                break
            time.sleep(5)
        st, att, tok, wid, s_at, f_at = rows(
            f"SELECT status, attempts, token, coalesce(worker_id, '-'), coalesce(extract(epoch from started_at), 0), "
            f"coalesce(extract(epoch from finished_at), 0) FROM tasks WHERE id = {tid}")[0]
        cnt, workers = rows(f"SELECT count(*), coalesce(string_agg(worker_id, ','), '-') "
                            f"FROM task_results WHERE task_id = {tid}")[0]
        t_kill_rel = killed["t_kill"] - t_first
        print(f"\n=== Сценарій Ф2 (на {t_kill_rel:.0f}-й секунді від першої задачі) ===")
        print(f"Воркер, сесію бази якого завершено: {killed['pod']}")
        print("Рядки журналу цього воркера про перепідключення:")
        for l in killed.get("reconnect") or ["  (не знайдено в журналах пода)"]:
            print("  " + l)
        print(f"Задача, що оброблялась: id = {tid}; до відмови attempts = {killed['attempts']}, token = {killed['token']}")
        print(f"Після прогону: status = {st}, attempts = {att}, token = {tok}, воркер, що завершив: {wid}")
        if int(att) > killed["attempts"] and float(s_at):
            print(f"Повторно захоплена через {float(s_at) - killed['t_kill']:.1f} с після відмови")
        from datetime import datetime
        wl = sh(["kubectl", "logs", "deploy/web", "--timestamps", "--tail=-1"], check=False)
        for mm in re.finditer(rf"^(\S+) reaper: задача {tid} \(attempts=(\d+)\) -> (\w+)", wl, flags=re.M):
            ts = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*Z$", r"\1+00:00", mm[1])).timestamp()
            if ts >= killed["t_kill"]:
                print(f"Повернуто в чергу (reaper у web) через {ts - killed['t_kill']:.1f} с після відмови: статус {mm[3]}")
                break
        if float(f_at):
            print(f"Завершено через {float(f_at) - killed['t_kill']:.1f} с після відмови")
        print(f"Рядків у task_results для задачі {tid}: {cnt} (воркер: {workers})")
        print("Рядки журналу пода про захоплення цієї задачі:")
        for l in killed.get("claims") or ["  (немає)"]:
            print("  " + l)
        summary["f2"] = {"task": tid, "pod": killed["pod"], "attempts_before": killed["attempts"],
                         "token_before": killed["token"], "status": st, "attempts_after": int(att),
                         "token_after": int(tok), "finished_by": wid, "result_rows": int(cnt),
                         "reconnect": killed.get("reconnect")}

    t_end = t_first + DURATION + TAIL + 5
    tq = prom_range("sum(tasks_queued)", t_first - 10, t_end)
    tr = prom_range("sum(tasks_running)", t_first - 10, t_end)
    fig, ax = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    for a, data, title, color in ((ax[0], tq, "tasks_queued (у черзі)", "tab:blue"),
                                  (ax[1], tr, "tasks_running (виконується)", "tab:orange")):
        if data:
            a.plot([t - t_first for t in data[0]], data[1], color=color)
        a.set_ylabel(title)
    sec = np.arange(len(bins))
    ax[2].plot(sec, bins, color="lightgray", label="за секунду")
    smooth = np.convolve(bins, np.ones(5) / 5, mode="same")
    ax[2].plot(sec, smooth, color="tab:green", label="ковзне середнє, 5 с")
    ax[2].axhline(rate, color="gray", linestyle=":", label=f"λ = {rate}")
    ax[2].set_ylabel("завершено задач за секунду")
    ax[2].set_xlabel("час від початку створення задач, с")
    ax[2].legend(loc="upper right")
    for a in ax:
        a.axvline(DURATION, color="gray", linestyle="--", alpha=0.7)
        if t_kill_rel is not None:
            a.axvline(t_kill_rel, color="red", linestyle="--",
                      label="відмова Ф2" if a is ax[0] else None)
        a.grid(alpha=0.3)
    if t_kill_rel is not None:
        ax[0].legend(loc="upper left")
    ax[0].set_title(f"FAIL_RATE = {F}, λ = {rate} задач/с (червона лінія – Ф2, сіра – кінець створення задач)",
                    fontsize=10)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "step9_chart.png"), dpi=150)
    with open(os.path.join(OUT, "step9_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1, default=str)
    np.savetxt(os.path.join(OUT, "step9_done_per_second.csv"), bins, fmt="%d", header="done_per_second")
    print("\nЗбережено: results/tasks/step9_summary.json, step9_chart.png, step9_done_per_second.csv")


if __name__ == "__main__":
    main()