import csv
import os
import statistics
import sys

WARMUP, DURATION = 10.0, 30.0

for path in sys.argv[1:]:
    with open(path, newline="") as f:
        recs = [(float(r["scheduled_s"]), float(r["sent_s"]), float(r["done_s"]), int(r["status"]))
                for r in csv.DictReader(f)]
    m = [r for r in recs if WARMUP <= r[0] < WARMUP + DURATION]
    wait_gen = statistics.median(r[1] - r[0] for r in m)
    wait_srv = statistics.median(r[2] - r[1] for r in m)
    events = sorted([(r[1], 1) for r in recs] + [(r[2], -1) for r in recs])
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    err = [r[2] - r[1] for r in m if r[3] == 0]
    fast = sum(1 for x in err if x < 10)
    print(f"{os.path.basename(path)}: очікування в генераторі p50 = {wait_gen:.2f} с, "
          f"від відправки до відповіді p50 = {wait_srv:.2f} с, макс. одночасно відправлених = {peak}, "
          f"код 0: {len(err)} (з них швидше за 10 с: {fast})")