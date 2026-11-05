import json, os, urllib.parse, urllib.request
import matplotlib.pyplot as plt

csv = r"results\step\k8s_n4_seconds.csv"
end = os.path.getmtime(csv) + 10
start = end - 200
q = 'sum by (pod) (rate(http_requests_total{pod=~"web-.*"}[15s]))'
url = "http://localhost:9090/api/v1/query_range?" + urllib.parse.urlencode(
    {"query": q, "start": start, "end": end, "step": 5})
data = json.load(urllib.request.urlopen(url))["data"]["result"]

plt.figure(figsize=(9, 4.5))
for s in data:
    plt.plot([float(p[0]) - start for p in s["values"]],
             [float(p[1]) for p in s["values"]], label=s["metric"]["pod"])
plt.xlabel("час від початку вікна, с")
plt.ylabel("запитів/с")
plt.title("Запити за секунду на под, N = 4")
plt.legend()
plt.grid(alpha=0.3)
plt.savefig(r"results\step\k8s_n4_pods.png", dpi=150, bbox_inches="tight")