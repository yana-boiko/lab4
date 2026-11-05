"""Статистика сходинки за даними Prometheus: p99 сервера, дроселювання CPU, розподіл запитів по подах."""
import csv
import json
import os
import sys
import urllib.parse
import urllib.request

PROM = "http://127.0.0.1:9090"
PATH = "/lab-results/{id}"
HERE = os.path.dirname(os.path.abspath(__file__))
INF = float("inf")


def query(expr, t):
    url = PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": t})
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]["result"]


def buckets_by_pod(t, flt):
    res = {}
    for s in query(f"sum by (pod, le) (http_request_duration_seconds_bucket{{{flt}}})", t):
        res.setdefault(s["metric"]["pod"], {})[float(s["metric"]["le"])] = float(s["value"][1])
    return res


def counter_by_pod(name, t):
    expr = f'sum by (pod) ({name}{{namespace="default", container="web"}})'
    return {s["metric"]["pod"]: float(s["value"][1]) for s in query(expr, t)}


def p99(delta):
    """delta: {верхня межа бакета, с: накопичена кількість}. Лінійна інтерполяція всередині бакета."""
    les = sorted(delta)
    r = 0.99 * delta[les[-1]]
    prev_b, prev_c = 0.0, 0.0
    for b in les:
        c = delta[b]
        if r <= c:
            if b == INF:
                return f"> {prev_b * 1000:g} мс"
            est = prev_b + (b - prev_b) * (r - prev_c) / (c - prev_c)
            return (f"{est * 1000:.1f} мс (бакет ({prev_b * 1000:g}; {b * 1000:g}], "
                    f"похибка до {(b - prev_b) * 1000:g} мс)")
        prev_b, prev_c = b, c
    return "немає даних"


def main():
    label = sys.argv[1]
    with open(os.path.join(HERE, "..", "results", "step", f"{label}_steps.csv"), newline="") as f:
        steps = list(csv.DictReader(f))
    if len(sys.argv) > 2:
        s = next(x for x in steps if int(x["step"]) == int(sys.argv[2]))
    else:
        s = max(steps, key=lambda x: float(x["achieved_rps"]))
    t0, t1 = int(s["window_start_epoch"]), int(s["window_end_epoch"])
    dur = t1 - t0

    flt = f'namespace="default", path="{PATH}"'
    b0, b1 = buckets_by_pod(t0, flt), buckets_by_pod(t1, flt)
    if not b1:  # у гістограми може не бути мітки path
        flt = 'namespace="default"'
        b0, b1 = buckets_by_pod(t0, flt), buckets_by_pod(t1, flt)
        print("(у гістограми немає мітки path: враховано всі запити сервісу)")
    if not b1:
        raise SystemExit("Prometheus не повернув ряди гістограми: перевір, чи працює http://127.0.0.1:9090")
    thr0, thr1 = counter_by_pod("container_cpu_cfs_throttled_periods_total", t0), counter_by_pod("container_cpu_cfs_throttled_periods_total", t1)
    per0, per1 = counter_by_pod("container_cpu_cfs_periods_total", t0), counter_by_pod("container_cpu_cfs_periods_total", t1)

    print(f"Прогін {label}, сходинка {s['step']} (задано {s['target_rps']} запитів/с), вікно {dur} с")
    print(f"Клієнт: досягнуто {s['achieved_rps']} запитів/с (розкид ±{s['achieved_std']}), "
          f"p99 клієнта {s['p99_ms']} мс\n")

    total = {}
    rows = []
    for pod in sorted(b1):
        d = {le: b1[pod][le] - b0.get(pod, {}).get(le, 0.0) for le in b1[pod]}
        for le, v in d.items():
            total[le] = total.get(le, 0.0) + v
        rps = d[INF] / dur
        dp = per1.get(pod, 0.0) - per0.get(pod, 0.0)
        thr = 100 * (thr1.get(pod, 0.0) - thr0.get(pod, 0.0)) / dp if dp else float("nan")
        rows.append((pod, rps, p99(d), thr))
        print(f"{pod}: {rps:7.1f} запитів/с | p99 сервера {p99(d)} | дроселювання CPU {thr:.1f} %")

    rates = [r[1] for r in rows]
    thrs = [r[3] for r in rows]
    hi, lo = max(rows, key=lambda r: r[1]), min(rows, key=lambda r: r[1])
    print(f"\nУсі поди разом: {sum(rates):.1f} запитів/с, p99 сервера {p99(total)}")
    print(f"Дроселювання CPU: середнє за подами {sum(thrs) / len(thrs):.1f} %, найбільше {max(thrs):.1f} %")
    print(f"Дисбаланс I = X_max / X_avg = {max(rates) / (sum(rates) / len(rates)):.3f}")
    print(f"Найзавантаженіший под {hi[0]}: {hi[1]:.1f} запитів/с, p99 сервера {hi[2]}, дроселювання {hi[3]:.1f} %")
    print(f"Найменш завантажений под {lo[0]}: {lo[1]:.1f} запитів/с, p99 сервера {lo[2]}, дроселювання {lo[3]:.1f} %")


if __name__ == "__main__":
    main()