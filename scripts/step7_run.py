"""Крок 7: закон Літтла для черги (сталий режим) і поведінка черги під перевантаженням."""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from step6_run import sh, sql, wait_ready

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
R = 4
PROM = "http://localhost:9090"
URL = "http://localhost:8080"


def prom_instant(query, t):
    url = PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": t})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            res = json.load(r)["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception as e:
        print(f"  (Prometheus недоступний: {e})")
        return None


def f(q):
    return float(sql(q))


def main():
    mode = sys.argv[1]
    X = json.load(open(os.path.join(OUT, "step6_fit.json"), encoding="utf-8"))["X"]
    xr = X[-1]
    if mode == "stable":
        k, dur, win, label = 0.7, 180, 120, "step7_stable"
    elif mode == "overload":
        k, dur, win, label = 1.3, 120, None, "step7_overload"
    else:
        raise SystemExit("Режим: stable або overload")
    rate = round(k * xr, 2)
    print(f"=== Крок 7, режим {mode}: X({R}) = {xr:.2f}, інтенсивність {k} · X({R}) = {rate} задач/с, {dur} с ===")
    wait_ready(R)
    sql("TRUNCATE tasks, task_results RESTART IDENTITY")
    time.sleep(8)

    subprocess.run([sys.executable, os.path.join(ROOT, "loadgen", "taskload.py"), "--url", URL,
                    "--rate", str(rate), "--duration", str(dur), "--label", label], check=True)
    live_q = sql("SELECT count(*) FROM tasks WHERE status = 'queued'")
    live_r = sql("SELECT count(*) FROM tasks WHERE status = 'running'")
    t_now = time.time()
    print(f"Одразу після завершення генератора: у черзі {live_q}, виконується {live_r}")

    deadline = time.time() + 900
    last_print = 0
    while sql("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running')") != "0":
        if time.time() > deadline:
            print("Черга не спорожніла за 900 с")
            break
        if mode == "overload" and time.time() - last_print > 15:
            last_print = time.time()
            print(f"  у черзі: {sql('SELECT count(*) FROM tasks WHERE status = chr(113)||chr(117)||chr(101)||chr(117)||chr(101)||chr(100)')}")
        time.sleep(0.5)

    t_first, t_last = [float(v) for v in sql(
        "SELECT min(extract(epoch from created_at)), max(extract(epoch from created_at)) FROM tasks").split("|")]
    n = int(sql("SELECT count(*) FROM tasks"))
    stat = sql("SELECT status || '=' || count(*) FROM tasks GROUP BY status ORDER BY status").replace("\n", ", ")
    att = int(sql("SELECT count(*) FROM tasks WHERE attempts > 1"))
    multi = int(sql("SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id "
                    "HAVING count(*) > 1) t"))
    print(f"\nЗадач створено: {n}; за статусами: {stat}; з attempts > 1: {att}; з кількома результатами: {multi}")
    if att:
        print("УВАГА: є задачі, захоплені більше одного разу; точне L_q за таблицею не застосовне")
    summary = {"mode": mode, "rate": rate, "X_R": xr, "n": n, "status": stat, "attempts_gt1": att}

    if mode == "stable":
        a, b = t_last - win, t_last
        ta, tb = f"to_timestamp({a})", f"to_timestamp({b})"
        lam = f(f"SELECT count(*) FROM tasks WHERE created_at >= {ta} AND created_at < {tb}") / win
        wq = f(f"SELECT avg(extract(epoch from (started_at - created_at))) FROM tasks "
               f"WHERE created_at >= {ta} AND created_at < {tb}")
        mid = (a + b) / 2
        wq1 = f(f"SELECT avg(extract(epoch from (started_at - created_at))) FROM tasks "
                f"WHERE created_at >= {ta} AND created_at < to_timestamp({mid})")
        wq2 = f(f"SELECT avg(extract(epoch from (started_at - created_at))) FROM tasks "
                f"WHERE created_at >= to_timestamp({mid}) AND created_at < {tb}")
        lq_tab = f("SELECT sum(greatest(0, extract(epoch from (least(started_at, %s) - greatest(created_at, %s))))) / %s "
                   "FROM tasks" % (tb, ta, win))
        lq_prom = prom_instant(f"avg(avg_over_time(tasks_queued[{win}s]))", b)
        law = lam * wq
        print(f"\n=== Результат: сталий режим, вікно останніх {win} с ===")
        print(f"λ = {lam:.2f} задач/с (створено в вікні: {round(lam * win)})")
        print(f"W_q = {wq:.4f} с ({wq * 1000:.1f} мс); перша половина вікна {wq1 * 1000:.1f} мс, "
              f"друга {wq2 * 1000:.1f} мс")
        print(f"λ · W_q = {law:.4f}")
        if lq_prom is not None:
            print(f"L_q (середнє tasks_queued за вікно, Prometheus) = {lq_prom:.4f}; "
                  f"розбіжність = {100 * abs(law - lq_prom) / lq_prom:.1f} %")
        print(f"L_q (точне за таблицею tasks) = {lq_tab:.4f}; розбіжність = {100 * abs(law - lq_tab) / lq_tab:.1f} %")
        summary.update(lam=lam, Wq=wq, Wq_half1=wq1, Wq_half2=wq2, law=law, Lq_prom=lq_prom, Lq_table=lq_tab)
    else:
        q_end = int(sql(f"SELECT count(*) FROM tasks WHERE created_at <= to_timestamp({t_last}) "
                        f"AND started_at > to_timestamp({t_last})"))
        lam = n / (t_last - t_first)
        thr = f(f"SELECT count(*) FROM tasks WHERE finished_at >= to_timestamp({t_first + 10}) "
                f"AND finished_at <= to_timestamp({t_last})") / (t_last - t_first - 10)
        wq_first = f("SELECT avg(extract(epoch from (started_at - created_at))) FROM "
                     "(SELECT started_at, created_at FROM tasks ORDER BY created_at LIMIT 100) t")
        wq_last = f("SELECT avg(extract(epoch from (started_at - created_at))) FROM "
                    "(SELECT started_at, created_at FROM tasks ORDER BY created_at DESC LIMIT 100) t")
        g = prom_instant("tasks_queued", t_now)
        print(f"\n=== Результат: перевантаження ===")
        print(f"Фактична інтенсивність створення: {lam:.2f} задач/с (задано {rate})")
        print(f"Швидкість виконання за час прогону (від 10-ї секунди): {thr:.2f} задач/с (X({R}) зі кроку 6 = {xr:.2f})")
        print(f"Довжина черги на кінець прогону (момент створення останньої задачі, за таблицею): {q_end}")
        print(f"Очікувана за (λ − X) · T: {(lam - xr) * (t_last - t_first):.0f}")
        if g is not None:
            print(f"tasks_queued у Prometheus одразу після завершення генератора: {g:.0f}")
        print(f"W_q за першими 100 задачами: {wq_first:.3f} с; за останніми 100 задачами: {wq_last:.3f} с")
        print(f"Оцінка W_q останніх задач за довжиною черги / X({R}): {q_end / xr:.1f} с")
        summary.update(lam=lam, throughput=thr, queue_end=q_end, Wq_first100=wq_first, Wq_last100=wq_last)

    rows = [r.split("|") for r in sql("SELECT extract(epoch from created_at), extract(epoch from started_at) "
                                      "FROM tasks").splitlines()]
    cr = np.array([float(r[0]) for r in rows]) - t_first
    st = np.array([float(r[1]) for r in rows]) - t_first
    grid = np.arange(0, st.max() + 1, 1.0)
    qlen = np.array([(cr <= s).sum() - (st <= s).sum() for s in grid])
    plt.figure(figsize=(9, 4.5))
    plt.plot(grid, qlen)
    plt.axvline(dur, color="gray", linestyle="--", label="кінець створення задач")
    if win:
        plt.axvspan(dur - win, dur, color="tab:orange", alpha=0.15, label=f"вікно вимірювання {win} с")
    plt.xlabel("час від початку створення задач, с")
    plt.ylabel("задач у черзі (queued)")
    plt.title(f"Довжина черги: {k} · X({R}) = {rate} задач/с")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    png = os.path.join(OUT, f"step7_{mode}_queue.png")
    plt.savefig(png, dpi=150)
    with open(os.path.join(OUT, f"step7_{mode}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(f"\nЗбережено: results/tasks/step7_{mode}.json і step7_{mode}_queue.png")


if __name__ == "__main__":
    main()