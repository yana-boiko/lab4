import numpy as np
import matplotlib.pyplot as plt

N = np.array([1, 2, 3, 4])
data = {
    "Compose": ([313.2, 496.0, 728.3, 889.8], [63.6, 68.3, 68.7, 66.6], "tab:blue"),
    "Kubernetes": ([312.9, 434.8, 609.1, 813.5], [19.2, 62.2, 60.0, 64.6], "tab:red"),
}
n = np.linspace(1, 6, 200)

plt.figure(figsize=(9, 5))
plt.plot(n, n, "--", color="gray", label="ідеал C = N")
for name, (X, err, color) in data.items():
    X = np.array(X)
    err = np.array(err)
    C = X / X[0]
    u = (N - 1)[1:]
    v = (N * (N - 1))[1:]
    y = (N / C - 1)[1:]
    d = (u @ u) * (v @ v) - (u @ v) ** 2
    sigma = ((v @ v) * (u @ y) - (u @ v) * (v @ y)) / d
    kappa = ((u @ u) * (v @ y) - (u @ v) * (u @ y)) / d
    print(name, "C =", C.round(3), "сирі σ =", round(sigma, 4), "κ =", round(kappa, 4))
    if kappa < 0:
        kappa = 0
        sigma = (u @ y) / (u @ u)
    elif sigma < 0:
        sigma = 0
        kappa = (v @ y) / (v @ v)
    nstar = np.sqrt((1 - sigma) / kappa) if kappa > 0 else None
    print(name, "σ =", round(sigma, 4), "κ =", round(kappa, 4), "N* =", nstar,
          "λ/σ =", round(X[0] / sigma, 1) if sigma > 0 else None)
    plt.errorbar(N, C, yerr=err / X[0], fmt="o", color=color, capsize=4, label=name + " (виміряно)")
    plt.plot(n, n / (1 + sigma * (n - 1) + kappa * n * (n - 1)), color=color,
             label=f"{name}: σ={sigma:.3f}, κ={kappa:.3f}")

plt.xlabel("N, кількість екземплярів")
plt.ylabel("C(N) = X(N) / X(1)")
plt.title("Універсальний закон масштабованості: Compose і Kubernetes")
plt.legend()
plt.grid(alpha=0.3)
plt.savefig(r"results\step\usl_k8s_vs_compose.png", dpi=150, bbox_inches="tight")