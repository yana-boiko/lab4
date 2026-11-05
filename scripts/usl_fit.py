import numpy as np
import matplotlib.pyplot as plt

N = np.array([1, 2, 3, 4])
X = np.array([313.2, 496.0, 728.3, 889.8])
S = np.array([63.6, 68.3, 68.7, 66.6])

lam = X[0]
C = X / lam
print("N | X(N) | C(N)")
for n, x, c in zip(N, X, C):
    print(f"{n} | {x:.1f} | {c:.3f}")

n = N[1:]
u = n - 1
v = n * (n - 1)
y = n / C[1:] - 1

Su2, Sv2, Suv = (u*u).sum(), (v*v).sum(), (u*v).sum()
Suy, Svy = (u*y).sum(), (v*y).sum()
den = Su2 * Sv2 - Suv**2

sigma = (Sv2 * Suy - Suv * Svy) / den
kappa = (Su2 * Svy - Suv * Suy) / den
print(f"\nпочаткові оцінки: sigma={sigma:.4f}, kappa={kappa:.5f}")

if sigma < 0 or kappa < 0:
    if kappa < 0:
        kappa = 0.0
        sigma = Suy / Su2
    else:
        sigma = 0.0
        kappa = Svy / Sv2
    print("одна з оцінок була відʼємна: перераховано за однопараметричною формулою")

print(f"sigma = {sigma:.4f}, kappa = {kappa:.5f}")
if kappa > 0 and sigma < 1:
    print(f"N* = {np.sqrt((1 - sigma) / kappa):.2f}")
else:
    print(f"N* не існує (kappa=0), X(N) наближається до lambda/sigma = {lam / sigma:.0f} запитів/с")

def usl(nn):
    return lam * nn / (1 + sigma * (nn - 1) + kappa * nn * (nn - 1))

nn = np.linspace(1, 8, 200)
plt.figure(figsize=(8, 5))
plt.errorbar(N, X, yerr=S, fmt="o", capsize=4, label="виміряне X(N) ± розкид")
plt.plot(nn, usl(nn), label=f"закон: σ={sigma:.3f}, κ={kappa:.4f}")
plt.plot(nn, lam * nn, "--", label="ідеал λN, λ = X(1)")
plt.xlabel("Кількість екземплярів N")
plt.ylabel("Пропускна здатність, запитів/с")
plt.title("Універсальний закон масштабованості")
plt.grid(alpha=0.3)
plt.legend()
plt.tight_layout()
plt.savefig(r"results\step\usl_fit.png", dpi=150)
print("Збережено: results\\step\\usl_fit.png")