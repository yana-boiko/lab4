"""Крок 6: C(W), σ, κ, W*, порівняння X(1) з оцінкою і діаграма X(W)."""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "results", "tasks")
T_POLL = 0.1
T_IO_STEP2, T_CPU_STEP2 = 0.012409, 0.048717
W = [1, 2, 4]
data = {w: json.load(open(os.path.join(OUT, f"step6_W{w}.json"), encoding="utf-8")) for w in W}
X = np.array([data[w]["X"] for w in W])
SD = np.array([data[w]["sd"] for w in W])
lam = X[0]
C = X / lam

print("W | X(W), задач/с | розкид | C(W) | p99 захоплення, мс | транзакцій/с | CPU бази, ядра | вікно, с")
for i, w in enumerate(W):
    p = data[w]["p99_claim_ms"]
    print(f"{w} | {X[i]:.2f} | {SD[i]:.2f} | {C[i]:.3f} | "
          f"{'> межі' if p is None else f'{p:.2f}'} | {data[w]['tps']:.0f} | "
          f"{data[w]['db_cpu_cores']:.2f} | {data[w]['window_s']}")

n = np.array([2, 4], dtype=float)
Cn = C[1:]
u, v, y = n - 1, n * (n - 1), n / Cn - 1
den = (u @ u) * (v @ v) - (u @ v) ** 2
sigma = ((v @ v) * (u @ y) - (u @ v) * (v @ y)) / den
kappa = ((u @ u) * (v @ y) - (u @ v) * (u @ y)) / den
print(f"\nСирі оцінки: σ = {sigma:.4f}, κ = {kappa:.5f}")
if sigma < 0 or kappa < 0:
    if kappa < 0:
        kappa, sigma = 0.0, (u @ y) / (u @ u)
    else:
        sigma, kappa = 0.0, (v @ y) / (v @ v)
    print("Одна з оцінок відʼємна: перераховано за однопараметричною формулою")
print(f"σ = {sigma:.4f}, κ = {kappa:.5f}")
if kappa > 0 and sigma < 1:
    wstar = (((1 - sigma) / kappa) ** 0.5)
    print(f"W* = sqrt((1 − σ) / κ) = {wstar:.2f}")
else:
    wstar = None
    print(f"κ = 0: W* не існує, X(W) наближається до λ/σ = {lam / sigma:.1f} задач/с" if sigma > 0
          else "σ = 0 і κ = 0: масштабування лінійне")

a = data[1]["avg_ms"]
t_io, t_cpu, t_claim = (a["fetch"] + a["store"]) / 1000, a["process"] / 1000, a["claim"] / 1000
p_run = 1 / (t_io + t_cpu + T_POLL / 2)
p_s2 = 1 / (T_IO_STEP2 + T_CPU_STEP2 + T_POLL / 2)
p_nopoll = 1 / (t_claim + t_io + t_cpu)
print(f"\nt_io = {t_io * 1000:.2f} мс, t_cpu = {t_cpu * 1000:.2f} мс (прогін W = 1), t_claim = {t_claim * 1000:.2f} мс")
print(f"1 / (t_io + t_cpu + T_poll/2), значення прогону:  {p_run:.2f} задач/с (X(1) відрізняється на "
      f"{100 * (X[0] - p_run) / p_run:+.1f} %)")
print(f"1 / (t_io + t_cpu + T_poll/2), значення кроку 2:  {p_s2:.2f} задач/с (X(1) відрізняється на "
      f"{100 * (X[0] - p_s2) / p_s2:+.1f} %)")
print(f"1 / (t_claim + t_io + t_cpu) (без очікування T_poll): {p_nopoll:.2f} задач/с (X(1) відрізняється на "
      f"{100 * (X[0] - p_nopoll) / p_nopoll:+.1f} %)")

json.dump({"X": X.tolist(), "sd": SD.tolist(), "C": C.tolist(), "sigma": float(sigma), "kappa": float(kappa),
           "Wstar": None if wstar is None else float(wstar), "pred_run": p_run, "pred_step2": p_s2,
           "pred_nopoll": p_nopoll}, open(os.path.join(OUT, "step6_fit.json"), "w"), indent=1)

nn = np.linspace(1, 8, 200)
curve = lam * nn / (1 + sigma * (nn - 1) + kappa * nn * (nn - 1))
plt.figure(figsize=(8, 5))
plt.errorbar(W, X, yerr=SD, fmt="o", capsize=4, label="виміряне X(W) ± розкид")
plt.plot(nn, curve, label=f"закон: σ = {sigma:.3f}, κ = {kappa:.4f}")
plt.plot(nn, lam * nn, "--", color="gray", label="ідеал λW, λ = X(1)")
plt.xlabel("Кількість воркерів W")
plt.ylabel("Пропускна здатність, задач/с")
plt.title("Пропускна здатність пулу воркерів: X(W)")
plt.grid(alpha=0.3)
plt.legend()
plt.tight_layout()
plt.savefig(os.path.join(OUT, "step6_usl.png"), dpi=150)
print("\nЗбережено: results/tasks/step6_usl.png, step6_fit.json")