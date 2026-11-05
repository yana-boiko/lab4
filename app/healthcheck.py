import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=3) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
