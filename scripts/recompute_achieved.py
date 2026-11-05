"""Перерахунок досягнутої інтенсивності з сирих CSV: лише відповіді 2xx у вікні (зауваження 2)."""
import csv, glob, os, re, statistics

W0, W1, DUR = 10, 40, 30


def achieved(path):
    with open(path, newline="") as f:
        n = sum(1 for r in csv.DictReader(f)
                if W0 <= float(r["done_s"]) < W1 and 200 <= int(r["status"]) < 300)
    return round(n / DUR, 1)


runs = {}
for path in sorted(glob.glob("results/raw/*.csv")):
    name = os.path.basename(path)
    a = achieved(path)
    print(f"{name:<26}{a}")
    m = re.match(r"web(\d+)_r(\d+)_rep\d+\.csv", name)
    if m:
        runs.setdefault((int(m[1]), int(m[2])), []).append(a)

print("\nМедіани:")
med = {k: statistics.median(v) for k, v in sorted(runs.items())}
for (n, rate), x in med.items():
    print(f"інстансів={n} задано={rate} досягнуто={x}")

print("\nПрискорення і Карп-Флатт:")
for rate in sorted({r for _, r in med}):
    x1, x2 = med.get((1, rate)), med.get((2, rate))
    if x1 and x2:
        s = x2 / x1
        print(f"задано={rate} X1={x1} X2={x2} S={s:.3f} e={(1 / s - 0.5) / 0.5:.3f}")