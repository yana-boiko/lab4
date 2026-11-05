"""Генератор навантаження з фіксованою швидкістю надходження запитів """
import argparse
import csv
import http.client
import json
import multiprocessing as mp
import os
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
TIMEOUT = 10.0


_local = threading.local()


def _conn(host, port):
    c = getattr(_local, "conn", None)
    if c is None:
        c = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        _local.conn = c
    return c


def http_request(host, port, method, path, body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    for attempt in range(2):
        c = _conn(host, port)
        try:
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            data = r.read()
            return r.status, data
        except Exception:
            c.close()
            _local.conn = None
            if attempt == 1:
                return 0, b""
    return 0, b""

def _worker(args):
    (host, port, paths, start_wall, total_s, rate, offset, mode, concurrency, threads) = args
    # ЗМІНА (зауваження 4): на Windows таймер за замовчуванням тікає кроком ~15.6 мс, тому
    # time.sleep прокидається лише на межах тіків і вибірка L потрапляє в одну й ту саму
    # фазу щодо відправлення запитів. Просимо крок 1 мс.
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    t0 = time.perf_counter() + (start_wall - time.time())
    records = []
    lock = threading.Lock()
    rnd = random.Random(os.getpid())
    # ЗМІНА (зауваження 4): лічильники відправлених і завершених запитів
    # та потік-семплер, який кожні 10 мс записує їх різницю
    state = [0, 0]  # [відправлено, завершено]
    samples = []
    stop = threading.Event()

    def sampler():
        # інтервал між вибірками випадковий (5-15 мс, у середньому 10 мс): строго
        # періодична вибірка збігається за фазою з сіткою відправлення запитів
        # і систематично занижує L
        srnd = random.Random()
        while not stop.is_set():
            with lock:
                n = state[0] - state[1]
            samples.append((time.perf_counter() - t0, n))
            time.sleep(srnd.uniform(0.005, 0.015))

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    def one(scheduled):
        path = rnd.choice(paths)
        with lock:
            state[0] += 1
        sent = time.perf_counter()
        status, _ = http_request(host, port, "GET", path)
        done = time.perf_counter()
        with lock:
            records.append((scheduled - t0, sent - t0, done - t0, status))
            state[1] += 1

    if mode == "open":
        n = int(total_s * rate)
        with ThreadPoolExecutor(max_workers=threads) as pool:
            for i in range(n):
                scheduled = t0 + offset + i / rate
                delay = scheduled - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)

                pool.submit(one, scheduled)
    else:
        end = t0 + total_s


        def loop():
            delay = t0 - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            while time.perf_counter() < end:
                one(time.perf_counter())

        ts = [threading.Thread(target=loop) for _ in range(concurrency)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    stop.set()
    sampler_thread.join()
    return records, samples


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def run_load(url, rate, duration, warmup, path=None, ids=None, mode="open", concurrency=0,
             processes=None, threads=200, label=None, instances=None, save=True):
    u = urlparse(url)
    host, port = u.hostname, u.port or 80
    paths = [path] if path else [f"/lab-results/{i}" for i in ids]
    processes = processes or os.cpu_count() or 1
    if mode == "open":
        processes = max(1, min(processes, int(rate)))
    else:
        processes = max(1, min(processes, concurrency))
    total = warmup + duration
    start_wall = time.time() + 1.0

    jobs = []
    for p in range(processes):
        if mode == "open":
            r = rate / processes
            offset = p / rate
            conc = 0
        else:
            r, offset = 0, 0
            conc = concurrency // processes + (1 if p < concurrency % processes else 0)
        jobs.append((host, port, paths, start_wall, total, r, offset, mode, conc, threads))

    with mp.Pool(processes) as pool:
        parts = pool.map(_worker, jobs)
    records = [r for recs, _ in parts for r in recs]


    w0, w1 = warmup, warmup + duration
    measured = [r for r in records if w0 <= r[0] < w1]
    # (зауваження 2): досягнута інтенсивність рахується лише за відповідями 2xx,
    # завершеними у вікні; обриви з'єднання і тайм-аути (код 0) та 4xx/5xx не враховуються
    completed = [r for r in records if w0 <= r[2] < w1 and 200 <= r[3] < 300]
    lat_ms = sorted((r[2] - r[0]) * 1000 for r in measured)
    non2xx = sum(1 for r in measured if not (200 <= r[3] < 300))


    # (зауваження 4): L вимірюється вибіркою, а не виводиться з інтервалів запитів.
    # L = сума по процесах середнього за вікно значення (відправлено - завершено).
    # Система для закону Літтла: від фактичної відправки запиту до відповіді,
    # тому lambda і W рахуються за моментом відправки (r[1]), а не запланованим.
    in_system = 0.0
    for _, smp in parts:
        vals = [n for t, n in smp if w0 <= t < w1]
        if vals:
            in_system += statistics.mean(vals)
    sent_in = [r for r in records if w0 <= r[1] < w1]
    lam = len(sent_in) / duration
    w_mean = statistics.mean([r[2] - r[1] for r in sent_in]) if sent_in else float("nan")
    gen_lag = statistics.mean([r[1] - r[0] for r in measured]) if measured else float("nan")

    summary = {
        "label": label or f"{mode}_r{rate}",
        "mode": mode,
        "instances": instances,
        "target_rps": rate if mode == "open" else None,
        "concurrency": concurrency if mode == "closed" else None,
        "achieved_rps": round(len(completed) / duration, 1),
        "p50_ms": round(percentile(lat_ms, 50), 1),
        "p95_ms": round(percentile(lat_ms, 95), 1),
        "p99_ms": round(percentile(lat_ms, 99), 1),
        "non2xx_pct": round(100 * non2xx / len(measured), 2) if measured else None,
        "requests": len(measured),
        "little_lambda": round(lam, 2),
        "little_W_s": round(w_mean, 5),
        "little_L_calc": round(lam * w_mean, 4),
        "little_L_measured": round(in_system, 4),
        "little_diff_pct": round(100 * abs(lam * w_mean - in_system) / in_system, 2) if in_system else None,
        "gen_lag_ms": round(gen_lag * 1000, 2),
        "processes": processes,
    }
    if save:
        os.makedirs(os.path.join(RESULTS_DIR, "raw"), exist_ok=True)
        with open(os.path.join(RESULTS_DIR, "raw", f"{summary['label']}.csv"), "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["scheduled_s", "sent_s", "done_s", "status"])
            for r in sorted(records):
                wr.writerow([f"{r[0]:.6f}", f"{r[1]:.6f}", f"{r[2]:.6f}", r[3]])
        os.makedirs(os.path.join(RESULTS_DIR, "inflight"), exist_ok=True)
        with open(os.path.join(RESULTS_DIR, "inflight", f"{summary['label']}.csv"), "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["process", "t_s", "in_flight"])
            for pi, (_, smp) in enumerate(parts):
                for t, n in smp:
                    wr.writerow([pi, f"{t:.4f}", n])
        with open(os.path.join(RESULTS_DIR, "runs.jsonl"), "a") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


def print_summary(s):
    target = s["target_rps"] if s["mode"] == "open" else f"closed c={s['concurrency']}"
    print(f"{s['label']:<22} задано={target!s:<8} досягнуто={s['achieved_rps']:<7} "
          f"p50={s['p50_ms']} p95={s['p95_ms']} p99={s['p99_ms']} мс  не-2xx={s['non2xx_pct']}%  "
          f"L={s['little_L_measured']}")


def get_ids(url, need=100):
    u = urlparse(url)
    status, data = http_request(u.hostname, u.port or 80, "GET", f"/lab-results?limit={need}")
    if status != 200:
        raise SystemExit(f"Не вдалося отримати записи: код {status}")
    ids = [it["id"] for it in json.loads(data)["items"]]
    if len(ids) < need:
        raise SystemExit(f"У базі {len(ids)} записів, потрібно {need}. Спершу виконай команду seed.")
    return ids


def cmd_seed(a):
    u = urlparse(a.url)
    host, port = u.hostname, u.port or 80
    status, data = http_request(host, port, "GET", "/lab-results?limit=1")
    have = json.loads(data)["total"] if status == 200 else 0
    tests = [("GLU", "mmol/L", 3.9, 6.1), ("HGB", "g/L", 120, 160), ("CHOL", "mmol/L", 3.0, 5.2),
             ("ALT", "U/L", 7, 56), ("CRP", "mg/L", 0, 5)]
    created = 0
    for i in range(have, 100):
        code, unit, lo, hi = tests[i % len(tests)]
        body = json.dumps({"patient_code": f"P-{i + 1:04d}", "test_code": code,
                           "value": round(random.uniform(lo, hi * 1.2), 2), "unit": unit,
                           "ref_low": lo, "ref_high": hi, "sample_date": "2026-10-01"})
        st, _ = http_request(host, port, "POST", "/lab-results", body)
        if st != 201:
            raise SystemExit(f"POST повернув {st}")
        created += 1
    print(f"Було записів: {have}, створено: {created}, разом: {have + created}")


def cmd_run(a):
    ids = None if a.path else get_ids(a.url)
    s = run_load(a.url, a.rate, a.duration, a.warmup, path=a.path, ids=ids,
                 label=a.label, instances=a.instances)
    print_summary(s)


def cmd_check(a):
    print("Перевірка генератора на /noop (балансувальник відповідає одразу, без сервісу і бази):")
    for rate in a.rates:
        s = run_load(a.url, rate, a.duration, a.warmup, path="/noop", label=f"noop_r{rate}")
        print_summary(s)


def cmd_series(a):
    ids = get_ids(a.url)
    rows = []
    for rate in a.rates:
        runs = []
        for rep in range(1, a.repeats + 1):
            s = run_load(a.url, rate, a.duration, a.warmup, ids=ids, instances=a.instances,
                         label=f"web{a.instances}_r{rate}_rep{rep}")
            print_summary(s)
            runs.append(s)
        med = {k: round(statistics.median([r[k] for r in runs]), 2)
               for k in ("achieved_rps", "p50_ms", "p95_ms", "p99_ms", "non2xx_pct", "little_L_measured")}
        rows.append({"instances": a.instances, "target_rps": rate, **med})
    path = os.path.join(RESULTS_DIR, f"series_web{a.instances}.csv")
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    print(f"\nМедіани з {a.repeats} повторів (збережено в {os.path.relpath(path)}):")
    print("інстанси | задано | досягнуто | p50, мс | p95, мс | p99, мс | не-2xx, %")
    for r in rows:
        print(f"{r['instances']:^8} | {r['target_rps']:^6} | {r['achieved_rps']:^9} | {r['p50_ms']:^7} | "
              f"{r['p95_ms']:^7} | {r['p99_ms']:^7} | {r['non2xx_pct']:^9}")


def cmd_closed(a):
    ids = get_ids(a.url)
    s = run_load(a.url, a.rate, a.duration, a.warmup, ids=ids, mode="closed",
                 concurrency=a.concurrency, instances=a.instances,
                 label=f"closed_web{a.instances}_c{a.concurrency}")
    print_summary(s)

def cmd_little(a):
    # (зауваження 4): L береться з вибірки results/inflight/<файл>.csv
    w0, w1 = a.warmup, a.warmup + a.duration
    with open(a.file, newline="") as f:
        recs = [(float(r["scheduled_s"]), float(r["sent_s"]), float(r["done_s"])) for r in csv.DictReader(f)]
    m = [r for r in recs if w0 <= r[1] < w1]
    lam = len(m) / a.duration
    w = statistics.mean(r[2] - r[1] for r in m)
    sample_file = os.path.join(RESULTS_DIR, "inflight", os.path.basename(a.file))
    per_proc = {}
    with open(sample_file, newline="") as f:
        for r in csv.DictReader(f):
            if w0 <= float(r["t_s"]) < w1:
                per_proc.setdefault(r["process"], []).append(int(r["in_flight"]))
    l_meas = sum(statistics.mean(v) for v in per_proc.values())
    print(f"{os.path.basename(a.file)}: λ = {lam:.2f} запитів/с, W = {w:.5f} с, "
          f"λ·W = {lam * w:.4f}, виміряне L = {l_meas:.4f}, "
          f"розбіжність = {100 * abs(lam * w - l_meas) / l_meas:.2f}%")


def main():
    p = argparse.ArgumentParser(description="Генератор навантаження з фіксованою швидкістю")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--url", default="http://127.0.0.1:8000")
        sp.add_argument("--duration", type=float, default=30, help="тривалість вимірювання, с")
        sp.add_argument("--warmup", type=float, default=10, help="прогрів, с (у результат не входить)")
        sp.add_argument("--instances", type=int, default=1, help="скільки вебінстансів у стенді (для підпису)")

    sp = sub.add_parser("seed", help="занести в базу 100 записів")
    sp.add_argument("--url", default="http://127.0.0.1:8000")
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser("run", help="один прогін")
    common(sp)
    sp.add_argument("--rate", type=int, required=True)
    sp.add_argument("--path", default=None, help="фіксований шлях замість /lab-results/{id}")
    sp.add_argument("--label", default=None)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("check", help="перевірка генератора на /noop")
    common(sp)
    sp.add_argument("--rates", type=int, nargs="+", default=[10, 100, 500])
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("series", help="серія 10/100/500 запитів/с, по 3 повтори")
    common(sp)
    sp.add_argument("--rates", type=int, nargs="+", default=[10, 100, 500])
    sp.add_argument("--repeats", type=int, default=3)
    sp.set_defaults(func=cmd_series)

    sp = sub.add_parser("closed", help="закритий контур з фіксованою кількістю одночасних запитів")
    common(sp)
    sp.add_argument("--rate", type=int, default=0, help="рівень відкритого контуру, з яким порівнюємо (для підпису)")
    sp.add_argument("--concurrency", type=int, required=True)
    sp.set_defaults(func=cmd_closed)

    sp = sub.add_parser("little", help="перерахувати закон Літтла із сирого CSV прогону")
    sp.add_argument("file")
    sp.add_argument("--duration", type=float, default=30)
    sp.add_argument("--warmup", type=float, default=10)
    sp.set_defaults(func=cmd_little)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()