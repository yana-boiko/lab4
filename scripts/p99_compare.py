"""Крок 9: порівняння p99 клієнта і p99 сервера на кожній сходинці прогону."""

import argparse
import csv
import json
import os
import urllib.parse
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def instant(prom, query):
    url = prom + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
    except Exception as e:
        raise SystemExit(f"Prometheus недоступний ({prom}): {e}\nПеревір: docker compose ps")
    if data.get("status") != "success":
        raise SystemExit(f"Prometheus повернув помилку: {data}")
    return data["data"]["result"]


def bucket_increases(prom, t_end, win_s, shift):
    q = ('sum by (le) (increase(http_request_duration_seconds_bucket{job="web"}[%ds] @ %d))'
         % (win_s, t_end - shift))
    res = instant(prom, q)
    d = {}
    for s in res:
        le = s["metric"]["le"]
        d[float("inf") if le == "+Inf" else float(le)] = float(s["value"][1])
    return d


def estimate_p99(d, q=0.99):
    """Повертає (значення в секундах або None, нижня межа, верхня межа, n)."""
    finite = sorted(b for b in d if b != float("inf"))
    cum, run = [], 0.0
    for b in finite:                 
        run = max(run, d[b])
        cum.append(run)
    n = max(d.get(float("inf"), 0.0), run)
    if n <= 0:
        return None, None, None, 0.0
    r = q * n
    prev_b, prev_c = 0.0, 0.0
    for b, c in zip(finite, cum):
        if r <= c and c > prev_c:
            return prev_b + (b - prev_b) * (r - prev_c) / (c - prev_c), prev_b, b, n
        prev_b, prev_c = b, c
    return None, finite[-1], float("inf"), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int, nargs="?", default=1, help="кількість екземплярів у прогоні (за замовчуванням 1)")
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--shift", type=float, default=0.0, help="поправка до часу Prometheus, с")
    a = ap.parse_args()

    path = os.path.join(ROOT, "results", "step", f"step_web{a.n}_steps.csv")
    with open(path, newline="") as f:
        steps = list(csv.DictReader(f))

    out_rows = []
    for s in steps:
        t0, t1 = int(s["window_start_epoch"]), int(s["window_end_epoch"])
        d = bucket_increases(a.prom, t1, t1 - t0, a.shift)
        val, lo, hi, n = estimate_p99(d)
        client = float(s["p99_ms"])
        if val is None and lo is None:
            server, bucket, err = "немає даних", "–", "–"
        elif val is None:
            server = f"> {lo * 1000:g} мс"
            bucket = f"({lo * 1000:g}; +∞)"
            err = "не обмежена"
        else:
            server = f"{val * 1000:.1f} мс"
            bucket = f"({lo * 1000:g}; {hi * 1000:g}] мс"
            err = f"≤ {(hi - lo) * 1000:g} мс"
        ratio = ""
        if val is not None and val > 0:
            ratio = f"{client / (val * 1000):.1f}"
        out_rows.append({"step": s["step"], "target_rps": s["target_rps"], "p99_client_ms": client,
                         "p99_server": server, "bucket": bucket, "max_error": err,
                         "n_observations": round(n), "client_to_server": ratio})

    print("\nсходинка | задано | p99 клієнта, мс | p99 сервера | бакет | макс. похибка | спостережень | клієнт/сервер")
    for r in out_rows:
        print(f"{r['step']:^8} | {r['target_rps']:^6} | {r['p99_client_ms']:^15} | {r['p99_server']:^11} | "
              f"{r['bucket']:^18} | {r['max_error']:^13} | {r['n_observations']:^12} | {r['client_to_server']!s:^13}")

    out = os.path.join(ROOT, "results", "step", f"p99_compare_web{a.n}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print("\nЗбережено:", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
