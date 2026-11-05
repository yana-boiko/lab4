"""Діаграми кроку 8: ступінчастий прогін за даними генератора і Prometheus на спільній осі часу.

Використання:  python scripts\\plot_step.py 1     (або 4)
Читає results/step/step_web<N>_seconds.csv і запитує Prometheus за той самий інтервал часу.
Результат: results/charts/step_web<N>.png
"""
import argparse
import csv
import json
import math
import os
import urllib.parse
import urllib.request
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SCRAPE_STEP = 5
WINDOW = "15s"

QUERIES = {
    "p99_server": 'histogram_quantile(0.99, sum by (le) (increase(http_request_duration_seconds_bucket{job="web"}[%s])))' % WINDOW,
    "rps": 'sum by (instance) (rate(http_requests_total{job="web",path="/lab-results/{item_id}"}[%s]))' % WINDOW,
    "cpu": 'rate(process_cpu_seconds_total{job="web"}[%s])' % WINDOW,
    "inprogress": 'http_requests_in_progress{job="web"}',
}


def prom_range(prom, query, t0, t1):
    url = prom + "/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": query, "start": t0, "end": t1, "step": SCRAPE_STEP})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
    except Exception as e:
        raise SystemExit(f"Prometheus недоступний ({prom}): {e}\nПеревір, що стенд працює: docker compose ps")
    if data.get("status") != "success":
        raise SystemExit(f"Prometheus повернув помилку: {data}")
    out = {}
    for s in data["data"]["result"]:
        name = s["metric"].get("instance", "").replace(":8000", "") or "усі"
        out[name] = ([float(t) for t, _ in s["values"]], [float(v) for _, v in s["values"]])
    return out


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int, help="кількість екземплярів у прогоні (1 або 4)")
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--shift", type=float, default=0.0,
                    help="поправка до часу Prometheus, с (якщо годинники Docker і Windows розходяться)")
    a = ap.parse_args()

    path = os.path.join(ROOT, "results", "step", f"step_web{a.n}_seconds.csv")
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    t_start = int(rows[0]["epoch_s"])
    t_end = int(rows[-1]["epoch_s"]) + 1
    gx = [int(r["second"]) + 0.5 for r in rows]
    target = [num(r["target_rps"]) for r in rows]
    achieved = [num(r["achieved_rps"]) for r in rows]
    p99_client = [num(r["p99_ms"]) for r in rows]
    steps = [int(r["step"]) for r in rows]

    prom = {}
    for key, q in QUERIES.items():
        prom[key] = prom_range(a.prom, q, t_start - a.shift - 5, t_end - a.shift + 5)

    def shift(series):
        ts, vs = series
        return [t - t_start + a.shift for t in ts], vs

    instances = sorted({i for k in ("rps", "cpu", "inprogress") for i in prom[k]})
    colors = {i: plt.cm.tab10(j % 10) for j, i in enumerate(instances)}

    fig, axs = plt.subplots(5, 1, figsize=(11, 15), sharex=True)
    ax_i, ax_p, ax_r, ax_c, ax_q = axs

    bounds = []
    for k in sorted(set(steps)):
        sec = [i for i, s in enumerate(steps) if s == k]
        bounds.append((k, sec[0], sec[-1] + 1))
    for ax in axs:
        for k, b0, b1 in bounds:
            if k % 2 == 1:
                ax.axvspan(b0, b1, color="0.93", zorder=0)
            ax.axvline(b0, color="0.8", lw=0.6, zorder=0)
        ax.grid(alpha=0.3)

    ax_i.step(gx, target, where="mid", color="tab:gray", ls="--", label="задана")
    ax_i.plot(gx, achieved, color="tab:blue", label="досягнута (відповіді 2xx)")
    ax_i.set_ylabel("запитів за секунду")
    ax_i.set_title(f"Ступінчастий прогін на {a.n} екземплярах", pad=24)
    ax_i.legend(loc="upper left")
    for k, b0, b1 in bounds:
        if k > 0:
            ax_i.text((b0 + b1) / 2, 1.02, f"{int(target[b0])}", transform=ax_i.get_xaxis_transform(),
                      ha="center", va="bottom", fontsize=8, color="0.4")
        else:
            ax_i.text((b0 + b1) / 2, 1.02, "прогрів", transform=ax_i.get_xaxis_transform(),
                      ha="center", va="bottom", fontsize=8, color="0.4")

    # 2. p99 клієнта і p99 сервера
    ax_p.plot(gx, [v if v == v and v > 0 else math.nan for v in p99_client], color="tab:red", label="p99 клієнта (щосекунди)")
    if prom["p99_server"]:
        ts, vs = shift(next(iter(prom["p99_server"].values())))
        ax_p.plot(ts, [v * 1000 for v in vs], color="tab:green", marker="o", ms=3,
                  label="p99 сервера (вікно 15 с, крок 5 с)")
    ax_p.axhline(1024, color="tab:green", ls=":", lw=1)
    ax_p.text(0.995, 1024, "верхня межа гістограми 1024 мс", transform=ax_p.get_yaxis_transform(),
              ha="right", va="bottom", fontsize=8, color="tab:green")
    ax_p.set_yscale("log")
    ax_p.set_ylabel("p99, мс (лог. шкала)")
    ax_p.legend(loc="upper left")

    # 3-5. по кожному екземпляру
    for ax, key, label in ((ax_r, "rps", "запитів за секунду"),
                           (ax_c, "cpu", "CPU, ядер"),
                           (ax_q, "inprogress", "запитів в обробці")):
        for inst in instances:
            if inst in prom[key]:
                ts, vs = shift(prom[key][inst])
                ax.plot(ts, vs, color=colors[inst], label=inst)
        ax.set_ylabel(label)
        ax.legend(loc="upper left", ncol=2, fontsize=8)
    ax_c.axhline(1.0, color="k", ls=":", lw=1)
    ax_c.text(0.995, 1.0, "ліміт 1 CPU", transform=ax_c.get_yaxis_transform(), ha="right", va="bottom", fontsize=8)
    ax_r.set_title("За екземплярами (Prometheus)", fontsize=10)
    ax_q.set_xlabel("час від початку прогону, с")
    ax_q.set_xlim(0, t_end - t_start)

    fig.tight_layout()
    outdir = os.path.join(ROOT, "results", "charts")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"step_web{a.n}.png")
    fig.savefig(out, dpi=130)
    print("Збережено:", os.path.relpath(out, ROOT))
    print("Екземпляри в Prometheus за цей інтервал:", ", ".join(instances) or "немає даних")


if __name__ == "__main__":
    main()