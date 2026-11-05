import http.client
import json
import sys

HOST, PORT = "127.0.0.1", 8000
if len(sys.argv) > 1:
    HOST, PORT = sys.argv[1].split(":")[0], int(sys.argv[1].split(":")[1])


def call(step, method, path, body=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
    data = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    print(f"{step}. {method} {path}")
    if body is not None:
        print(f"   тіло запиту: {data}")
    print(f"   -> {resp.status} {resp.reason}  {raw if raw else '(без тіла)'}")
    return resp.status, (json.loads(raw) if raw else None)


record = {"patient_code": "P-9001", "test_code": "CYCLE", "value": 4.8, "unit": "mmol/L",
          "ref_low": 3.9, "ref_high": 6.1, "sample_date": "2026-10-11"}

status, created = call(1, "POST", "/lab-results", record)
assert status == 201, "очікувався 201"
rid = created["id"]
call(2, "GET", "/lab-results?test_code=CYCLE")
call(3, "GET", f"/lab-results/{rid}")
call(4, "PUT", f"/lab-results/{rid}", {**record, "value": 6.5, "sample_date": "2026-10-12"})
call(5, "GET", f"/lab-results/{rid}")
call(6, "DELETE", f"/lab-results/{rid}")
call(7, "DELETE", f"/lab-results/{rid}")
call(8, "GET", f"/lab-results/{rid}")
