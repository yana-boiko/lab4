import csv
import json
import urllib.parse
import urllib.request

import matplotlib.pyplot as plt
import numpy as np

PROM = "http://localhost:9090/api/v1/query_range"
R = 4
TARGET = 70
JUMP_START, JUMP_END = 30, 90
X = {1: 312.9, 2: 434.8, 3: 609.1, 4: 813.5}
step_dir = r"results\step"
runs = [("k8s_profile_n4", "без автомасштабування, 4 репліки"), ("k8s_hpa_n4", "з HPA")]
queries = {
    "ready": 'sum(kube_deployment_status_replicas_ready{deployment="web"})',
    "cpu": 'avg(rate(container_cpu_usage_seconds_total{pod=~"web-.*",container="web"}[15s])) / 0.5 * 100',
}
hpa_query = 'sum(kube_horizontalpodautoscaler_status_desired_replicas{horizontalpodautoscaler="web"})'

with open(rf"{step_dir}\k8s_profile_n4_segments.csv") as f:
    seg = list(csv.DictReader(f))
p99_0 = float(seg[0]["p99_ms"])
p99_jump = float(seg[1]["p99_ms"])
print(f"p99₀ (перший відрізок, без автомасштабування) = {p99_0} мс; поріг порушення 2·p99₀ = {2 * p99_0:.1f} мс")
print(f"p99 на рівні стрибка (крок 10) = {p99_jump} мс; поріг відновлення 1,2·p99 = {1.2 * p99_jump:.1f} мс\n")

data = {}
for label, title in runs:
    with open(rf"{step_dir}\{label}_seconds.csv") as f:
        rows = list(csv.DictReader(f))
    total = len(rows)
    epoch = [int(r["epoch_s"]) for r in rows]
    z = np.array([float(r["target_rps"]) for r in rows])
    ach = np.array([float(r["achieved_rps"]) for r in rows])
    p99 = np.array([float(r["p99_ms"]) if r["p99_ms"] != "" else np.nan for r in rows])
    non2 = sum(int(r["non2xx"]) for r in rows)
    conn = sum(int(r["conn_errors"]) for r in rows)

    qs = dict(queries)
    if "hpa" in label:
        qs["desired"] = hpa_query
    series = {}
    for name, q in qs.items():
        url = PROM + "?" + urllib.parse.urlencode({"query": q, "start": epoch[0], "end": epoch[-1], "step": 1})
        res = json.load(urllib.request.urlopen(url))["data"]["result"]
        arr = np.full(total, np.nan)
        if not res:
            print(f"УВАГА: Prometheus не повернув ряд {name} для {label}")
        else:
            for ts, v in res[0]["values"]:
                i = int(round(float(ts))) - epoch[0]
                if 0 <= i < total:
                    arr[i] = float(v)
        for i in range(1, total):
            if np.isnan(arr[i]):
                arr[i] = arr[i - 1]
        for i in range(total - 2, -1, -1):
            if np.isnan(arr[i]):
                arr[i] = arr[i + 1]
        series[name] = arr

    s = np.round(series["ready"])
    d = np.array([next((n for n in range(1, R + 1) if X[n] >= zz), R) for zz in z])
    viol = int(np.sum(p99 > 2 * p99_0))
    print(f"=== {title} ({label}) ===")
    print(f"θ_U = {np.mean(s < d):.3f}   θ_O = {np.mean(s > d):.3f}")
    print(f"a_U = {np.mean(np.maximum(d - s, 0)):.3f}   a_O = {np.mean(np.maximum(s - d, 0)):.3f}")
    print(f"W = {int(s.sum())} репліко-секунд")
    print(f"секунд порушення порогу = {viol}")
    print(f"відповідей не 2xx = {non2}, помилок зʼєднання = {conn}")

    if "hpa" in label:
        des = series["desired"]
        ch = [t for t in range(JUMP_START, total) if des[t] != des[JUMP_START - 1]]
        mx = s.max()
        first_max = int(np.argmax(s >= mx))
        ok = None
        above = [t for t in range(JUMP_START, JUMP_END) if p99[t] > 1.2 * p99_jump]
        for t in range(above[0] if above else JUMP_START, JUMP_END - 9):
            if np.all(p99[t:t + 10] < 1.2 * p99_jump):
                ok = t
                break
        dec = [t for t in range(JUMP_END, total) if s[t] < s[JUMP_END - 1]]
        print("--- події кроку 11 ---")
        print("стрибок -> перша зміна бажаної кількості реплік:",
              f"{ch[0] - JUMP_START} с" if ch and ch[0] < JUMP_END else "не досягнуто")
        print("   (фактично найбільша кількість реплік стала готовою через", first_max - JUMP_START, "с після початку стрибка)")
        print(f"стрибок -> готовність найбільшої кількості реплік ({int(mx)}):",
              f"{first_max - JUMP_START} с" if mx > s[JUMP_START - 1] and JUMP_START <= first_max < JUMP_END else "не досягнуто")
        print("стрибок -> p99 нижчий за 1,2·p99 10 с поспіль:",
              f"{ok - JUMP_START} с" if ok is not None else "не досягнуто")
        print("кінець стрибка -> перше зменшення готових реплік:",
              f"{dec[0] - JUMP_END} с" if dec else "не досягнуто")
    print()
    data[label] = dict(z=z, ach=ach, p99=p99, d=d, s=s, cpu=series["cpu"])

fig, ax = plt.subplots(4, 2, figsize=(13, 12), sharex=True, sharey="row")
for j, (label, title) in enumerate(runs):
    D = data[label]
    t = np.arange(len(D["z"]))
    ax[0, j].plot(t, D["z"], "--", color="gray", label="задана")
    ax[0, j].plot(t, D["ach"], color="tab:blue", label="досягнута")
    ax[1, j].plot(t, D["p99"], color="tab:red")
    ax[1, j].set_yscale("log")
    ax[2, j].step(t, D["d"], where="post", color="gray", label="d(t) потрібні")
    ax[2, j].step(t, D["s"], where="post", color="tab:green", label="s(t) готові")
    ax[3, j].plot(t, D["cpu"], color="tab:purple")
    ax[3, j].axhline(TARGET, linestyle="--", color="gray")
    ax[0, j].set_title(title)
    ax[3, j].set_xlabel("час, с")
    for i in range(4):
        ax[i, j].axvline(JUMP_START, color="orange", alpha=0.5)
        ax[i, j].axvline(JUMP_END, color="orange", alpha=0.5)
        ax[i, j].grid(alpha=0.3)
ax[0, 0].set_ylabel("запитів/с")
ax[1, 0].set_ylabel("p99 клієнта, мс")
ax[2, 0].set_ylabel("репліки")
ax[3, 0].set_ylabel("CPU, % від requests")
ax[0, 0].legend()
ax[2, 0].legend()
plt.tight_layout()
plt.savefig(rf"{step_dir}\elasticity.png", dpi=150)