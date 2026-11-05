"""Крок 11: М4 — частка запитів з тривалістю обробки не більшою за поріг, залежно від заданої інтенсивності."""

import argparse
import csv
import os

import matplotlib.pyplot as plt

from p99_compare import ROOT, bucket_increases, estimate_p99


def read_steps(n):
    path = os.path.join(ROOT, "results", "step", f"step_web{n}_steps.csv")
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def pick_threshold(prom, shift):
    s = read_steps(1)[0]
    t0, t1 = int(s["window_start_epoch"]), int(s["window_end_epoch"])
    d = bucket_increases(prom, t1, t1 - t0, shift)
    val, lo, hi, n = estimate_p99(d)
    if val is None:
        raise SystemExit("Немає p99 сервера на першій сходинці: поріг визначити неможливо")
    bounds = sorted(b for b in d if b != float("inf"))
    best = min(bounds, key=lambda b: (abs(b - val), -b))
    print(f"p99 сервера на 1-й сходинці (N=1): {val * 1000:.1f} мс -> поріг {best * 1000:g} мс")
    return best


def m4_for_run(prom, n, thr, shift):
    rows = []
    for s in read_steps(n):
        t0, t1 = int(s["window_start_epoch"]), int(s["window_end_epoch"])
        d = bucket_increases(prom, t1, t1 - t0, shift)
        total = d.get(float("inf"), 0.0)
        c = next((v for b, v in d.items() if abs(b - thr) < 1e-9), None)
        if c is None or total <= 0:
            m4 = None
        else:
            m4 = c / total
        rows.append({"instances": n, "step": s["step"], "target_rps": int(s["target_rps"]),
                     "threshold_ms": thr * 1000, "n_observations": round(total),
                     "m4": "" if m4 is None else round(m4, 4)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--shift", type=float, default=0.0)
    a = ap.parse_args()

    thr = pick_threshold(a.prom, a.shift)
    all_rows = []
    plt.figure(figsize=(8, 5))
    for n in (1, 4):
        rows = m4_for_run(a.prom, n, thr, a.shift)
        all_rows += rows
        pts = [(r["target_rps"], r["m4"]) for r in rows if r["m4"] != ""]
        plt.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=f"{n} екз.")

    print("\nекз. | сходинка | задано | спостережень | М4")
    for r in all_rows:
        print(f"{r['instances']:^4} | {r['step']:^8} | {r['target_rps']:^6} | {r['n_observations']:^12} | {r['m4']}")

    out_csv = os.path.join(ROOT, "results", "step", "m4_web1_web4.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)

    plt.xlabel("Задана інтенсивність, запитів/с")
    plt.ylabel(f"М4: частка запитів з обробкою ≤ {thr * 1000:g} мс")
    plt.title("М4 залежно від заданої інтенсивності")
    plt.ylim(0, 1.05)
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    out_png = os.path.join(ROOT, "results", "step", "m4_plot.png")
    plt.savefig(out_png, dpi=150)
    print("\nЗбережено:", os.path.relpath(out_csv, ROOT), "і", os.path.relpath(out_png, ROOT))


if __name__ == "__main__":
    main()