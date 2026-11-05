"""Ступінчастий генератор навантаження з фіксованою швидкістю надходження (відкритий контур)."""
import argparse
import csv
import http.client
import json
import multiprocessing as mp
import os
import random
import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "step")
TIMEOUT = 5.0
TIMEOUT_CODE = -1
CONN_ERR_CODE = 0
TRANSIENT = 10
CONN_REQUESTS = 100  
_local = threading.local()


def request_once(host, port, path):
    for attempt in range(2):
        c = getattr(_local, "conn", None)
        if c is not None and _local.count >= CONN_REQUESTS:
            c.close()  # зʼєднання відпрацювало 100 запитів: закриваємо, нижче відкриється нове
            c = None
        reused = c is not None and c.sock is not None
        if c is None:
            c = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
            _local.conn = c
            _local.count = 0
        _local.count += 1
        try:
            c.request("GET", path)
            r = c.getresponse()
            r.read()
            return r.status
        except socket.timeout:
            c.close()
            _local.conn = None
            return TIMEOUT_CODE
        except (OSError, http.client.HTTPException):
            c.close()
            _local.conn = None
            if reused and attempt == 0:
                continue
            return CONN_ERR_CODE
    return CONN_ERR_CODE


def worker(p, nproc, host, port, paths, start_wall, segs, stop, ok_counts, conns, out_q):
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    t0 = time.perf_counter() + (start_wall - time.time())
    rnd = random.Random(os.getpid())
    records = []
    lock = threading.Lock()
    n_slots = len(ok_counts)

    def one(scheduled):
        path = rnd.choice(paths)
        sent = time.perf_counter()
        status = request_once(host, port, path)
        done = time.perf_counter()
        if 200 <= status < 300:
            idx = int(done - t0)
            if 0 <= idx < n_slots:
                with ok_counts.get_lock():
                    ok_counts[idx] += 1
        with lock:
            records.append((scheduled - t0, sent - t0, done - t0, status))

    # один потік = одне зʼєднання. Усі зʼєднання відкриваємо одразу, до початку прогону;
    # бар'єр змушує кожне із conns завдань виконатись у своєму потоці
    pool = ThreadPoolExecutor(max_workers=conns)
    barrier = threading.Barrier(conns)

    def open_conn():
        c = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        c.connect()
        _local.conn = c
        _local.count = 0
        barrier.wait()

    for f in [pool.submit(open_conn) for _ in range(conns)]:
        f.result()
    stopped = False
    for seg_start, dur, rate in segs:
        if stopped:
            break
        n = int(dur * rate)
        for i in range(p, n, nproc):
            if stop.is_set():
                stopped = True
                break
            scheduled = t0 + seg_start + i / rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            pool.submit(one, scheduled)
    pool.shutdown(wait=True, cancel_futures=stopped)
    out_q.put(records)


def fetch_ids(host, port, need=100):
    c = http.client.HTTPConnection(host, port, timeout=10)
    c.request("GET", f"/lab-results?limit={need}")
    r = c.getresponse()
    data = r.read()
    c.close()
    if r.status != 200:
        raise SystemExit(f"Не вдалося отримати записи: код {r.status}")
    ids = [it["id"] for it in json.loads(data)["items"]]
    if len(ids) < need:
        raise SystemExit(f"У базі {len(ids)} записів, потрібно {need}. Спершу виконай: python loadgen\\loadgen.py seed")
    return ids


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def fmt(x, nd=1):
    return "" if x != x else round(x, nd)


def run(a):
    u = urlparse(a.url)
    host, port = u.hostname, u.port or 80
    paths = [a.path] if a.path else [f"/lab-results/{i}" for i in fetch_ids(host, port)]
    nproc = min(a.processes or os.cpu_count() or 1, a.connections)
    # a.connections зʼєднань ділимо між процесами порівну
    conns_of = lambda p: a.connections // nproc + (1 if p < a.connections % nproc else 0)
    if a.profile:
        segs, t = [], 0.0
        for item in a.profile.split(","):
            dur, rate = item.split(":")
            segs.append((t, float(dur), float(rate)))
            t += float(dur)
        total = int(t)
        stop = mp.Event()
        ok_counts = mp.Array("i", total + 60)
        q = mp.Queue()
        start_wall = time.time() + 2.0
        procs = [mp.Process(target=worker, args=(p, nproc, host, port, paths, start_wall, segs, stop,
                                                 ok_counts, conns_of(p), q)) for p in range(nproc)]
        for pr in procs:
            pr.start()
        print(f"Прогін {a.label}: профіль {a.profile}, процесів {nproc}, зʼєднань {a.connections}")
        records = []
        for _ in procs:
            records.extend(q.get())
        for pr in procs:
            pr.join()

        sent = [0] * total
        ach = [0] * total
        non2 = [0] * total
        conn = [0] * total
        lat = [[] for _ in range(total)]
        for sched, s, d, st in records:
            si, di = int(s), int(d)
            if 0 <= si < total:
                sent[si] += 1
            if 0 <= di < total:
                if 200 <= st < 300:
                    ach[di] += 1
                elif st == CONN_ERR_CODE:
                    conn[di] += 1
                else:
                    non2[di] += 1
                if st != CONN_ERR_CODE:
                    lat[di].append((d - sched) * 1000)

        os.makedirs(RESULTS_DIR, exist_ok=True)
        sec_path = os.path.join(RESULTS_DIR, f"{a.label}_seconds.csv")
        with open(sec_path, "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["second", "segment", "target_rps", "sent_rps", "achieved_rps", "non2xx",
                         "conn_errors", "p50_ms", "p95_ms", "p99_ms", "epoch_s"])
            for sec in range(total):
                k = [i for i, sg in enumerate(segs, 1) if sg[0] <= sec < sg[0] + sg[1]][0]
                ls = sorted(lat[sec])
                wr.writerow([sec, k, segs[k - 1][2], sent[sec], ach[sec], non2[sec], conn[sec],
                             fmt(percentile(ls, 50)), fmt(percentile(ls, 95)), fmt(percentile(ls, 99)),
                             round(start_wall + sec)])

        rows = []
        for k, (s0, dur, rate) in enumerate(segs, 1):
            win = range(int(s0), int(s0 + dur))
            ls = sorted(x for s in win for x in lat[s])
            rows.append({
                "segment": k, "duration_s": dur, "target_rps": rate,
                "sent_rps": round(statistics.mean(sent[s] for s in win), 1),
                "achieved_rps": round(statistics.mean(ach[s] for s in win), 1),
                "p50_ms": fmt(percentile(ls, 50)), "p95_ms": fmt(percentile(ls, 95)),
                "p99_ms": fmt(percentile(ls, 99)),
                "non2xx": sum(non2[s] for s in win), "conn_errors": sum(conn[s] for s in win),
                "start_epoch": round(start_wall + s0), "end_epoch": round(start_wall + s0 + dur)})
        seg_path = os.path.join(RESULTS_DIR, f"{a.label}_segments.csv")
        with open(seg_path, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            wr.writerows(rows)

        print("\nвідрізок | тривалість, с | задано | надіслано | досягнуто | p50, мс | p95, мс | p99, мс | не-2xx | помилок з'єдн.")
        for r in rows:
            print(f"{r['segment']:^8} | {r['duration_s']:^13g} | {r['target_rps']:^6g} | {r['sent_rps']:^9} | "
                  f"{r['achieved_rps']:^9} | {r['p50_ms']!s:^7} | {r['p95_ms']!s:^7} | {r['p99_ms']!s:^7} | "
                  f"{r['non2xx']:^6} | {r['conn_errors']:^14}")
        print(f"Файли: {os.path.relpath(sec_path)}, {os.path.relpath(seg_path)}")
        return
    warmup, step_s = a.warmup, a.step_seconds

    def rate_of(k):
        return a.start_rate + (k - 1) * a.step_rate

    segs = [(0.0, warmup, a.start_rate)]
    for k in range(1, a.max_steps + 1):
        segs.append((warmup + (k - 1) * step_s, step_s, rate_of(k)))
    cap = int(warmup + a.max_steps * step_s) + 30

    stop = mp.Event()
    ok_counts = mp.Array("i", cap)
    q = mp.Queue()
    start_wall = time.time() + 2.0
    procs = [mp.Process(target=worker, args=(p, nproc, host, port, paths, start_wall, segs, stop,
                                             ok_counts, conns_of(p), q)) for p in range(nproc)]
    for pr in procs:
        pr.start()

    print(f"Прогін {a.label}: прогрів {warmup:g} с на {a.start_rate} запитів/с, далі сходинки по {step_s:g} с, "
          f"процесів {nproc}, зʼєднань {a.connections}, кожне закривається після {CONN_REQUESTS} запитів")
    below = 0
    done_steps = 0
    reason = f"виконано максимальну кількість сходинок ({a.max_steps})"
    for k in range(1, a.max_steps + 1):
        seg_start = warmup + (k - 1) * step_s
        seg_end = seg_start + step_s
        while time.time() < start_wall + seg_end + 1.0:
            time.sleep(0.05)
        target = rate_of(k)
        vals = [ok_counts[s] for s in range(int(seg_start + TRANSIENT), int(seg_end))]
        ach = statistics.mean(vals)
        done_steps = k
        print(f"  сходинка {k:>2}: задано {target:>5}, досягнуто {ach:7.1f} ({100 * ach / target:5.1f} %)")
        below = below + 1 if ach < 0.9 * target else 0
        if below >= 2:
            reason = "на двох сходинках поспіль досягнута інтенсивність нижча за 0,9 заданої"
            break
        if a.g and ach >= a.g:
            reason = f"сходинка досягла межі генератора G = {a.g:g}"
            break
    stop.set()
    records = []
    for _ in procs:
        records.extend(q.get())
    for pr in procs:
        pr.join()
    print(f"Прогін зупинено: {reason}")

    t_end = int(warmup + done_steps * step_s)
    sent = [0] * t_end
    ach = [0] * t_end
    non2 = [0] * t_end
    conn = [0] * t_end
    lat = [[] for _ in range(t_end)]
    for sched, s, d, st in records:
        si, di = int(s), int(d)
        if 0 <= si < t_end:
            sent[si] += 1
        if 0 <= di < t_end:
            if 200 <= st < 300:
                ach[di] += 1
            elif st == CONN_ERR_CODE:
                conn[di] += 1
            else:
                non2[di] += 1
            if st != CONN_ERR_CODE:
                lat[di].append((d - sched) * 1000)

    def step_of(sec):
        return 0 if sec < warmup else int((sec - warmup) // step_s) + 1

    os.makedirs(RESULTS_DIR, exist_ok=True)
    sec_path = os.path.join(RESULTS_DIR, f"{a.label}_seconds.csv")
    with open(sec_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["second", "step", "target_rps", "sent_rps", "achieved_rps", "ok_2xx", "non2xx",
                     "conn_errors", "p50_ms", "p95_ms", "p99_ms", "epoch_s"])
        for sec in range(t_end):
            k = step_of(sec)
            ls = sorted(lat[sec])
            wr.writerow([sec, k, a.start_rate if k == 0 else rate_of(k), sent[sec], ach[sec], ach[sec],
                         non2[sec], conn[sec], fmt(percentile(ls, 50)), fmt(percentile(ls, 95)),
                         fmt(percentile(ls, 99)), round(start_wall + sec)])

    steps = []
    for k in range(1, done_steps + 1):
        lo = int(warmup + (k - 1) * step_s + TRANSIENT)
        hi = int(warmup + k * step_s)
        win = range(lo, hi)
        ls = sorted(x for s in win for x in lat[s])
        n_ok, n_bad = sum(ach[s] for s in win), sum(non2[s] for s in win)
        steps.append({
            "step": k, "target_rps": rate_of(k),
            "sent_rps": round(statistics.mean(sent[s] for s in win), 1),
            "achieved_rps": round(statistics.mean(ach[s] for s in win), 1),
            "achieved_std": round(statistics.stdev([ach[s] for s in win]), 1) if len(win) > 1 else 0.0,
            "p50_ms": fmt(percentile(ls, 50)), "p95_ms": fmt(percentile(ls, 95)),
            "p99_ms": fmt(percentile(ls, 99)),
            "non2xx_pct": round(100 * n_bad / (n_ok + n_bad), 2) if n_ok + n_bad else "",
            "conn_errors": sum(conn[s] for s in win),
            # unix-час початку і кінця 20-секундного вікна: для запитів до Prometheus (p99 сервера, CPU)
            "window_start_epoch": round(start_wall + lo),
            "window_end_epoch": round(start_wall + hi),
        })
    st_path = os.path.join(RESULTS_DIR, f"{a.label}_steps.csv")
    with open(st_path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(steps[0].keys()))
        wr.writeheader()
        wr.writerows(steps)
    if a.save_raw:
        raw_path = os.path.join(RESULTS_DIR, f"{a.label}_raw.csv")
        with open(raw_path, "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["scheduled_s", "sent_s", "done_s", "status"])
            for r in sorted(records):
                wr.writerow([f"{r[0]:.6f}", f"{r[1]:.6f}", f"{r[2]:.6f}", r[3]])

    print("\nсходинка | задано | надіслано | досягнуто | розкид | p50, мс | p95, мс | p99, мс | не-2xx, % | помилок з'єдн.")
    for s in steps:
        print(f"{s['step']:^8} | {s['target_rps']:^6} | {s['sent_rps']:^9} | {s['achieved_rps']:^9} | "
              f"{s['achieved_std']:^6} | {s['p50_ms']!s:^7} | {s['p95_ms']!s:^7} | {s['p99_ms']!s:^7} | "
              f"{s['non2xx_pct']!s:^9} | {s['conn_errors']:^14}")
    best = max(steps, key=lambda s: s["achieved_rps"])
    print(f"\nX = {best['achieved_rps']} запитів/с (сходинка {best['step']}, задано {best['target_rps']}, "
          f"розкид ±{best['achieved_std']}). Ця ж величина є межею генератора G, якщо прогін проти /noop.")
    print(f"Файли: {os.path.relpath(sec_path)}, {os.path.relpath(st_path)}")


def main():
    p = argparse.ArgumentParser(description="Ступінчастий генератор навантаження (відкритий контур)")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--path", default=None, help="фіксований шлях (наприклад /noop) замість /lab-results/{id}")
    p.add_argument("--instances", type=int, default=1, help="кількість вебінстансів (для назви файлів)")
    p.add_argument("--label", default=None, help="назва прогону (за замовчуванням k8s_n<N> або step_noop)")
    p.add_argument("--warmup", type=float, default=10, help="прогрів, с")
    p.add_argument("--step-seconds", type=float, default=30, help="тривалість сходинки, с")
    p.add_argument("--start-rate", type=int, default=200, help="рівень прогріву і першої сходинки")
    p.add_argument("--step-rate", type=int, default=200, help="приріст інтенсивності між сходинками")
    p.add_argument("--max-steps", type=int, default=15, help="найбільша кількість сходинок")
    p.add_argument("--g", type=float, default=None, help="межа генератора G: зупинитись після сходинки, що її досягла")
    p.add_argument("--connections", type=int, default=32, help="постійних зʼєднань у всьому генераторі (8 · R, R = 4)")
    p.add_argument("--processes", type=int, default=None, help="процесів (за замовчуванням кількість ядер)")
    p.add_argument("--save-raw", action="store_true", help="зберегти сирі записи кожного запиту")
    p.add_argument("--profile", default=None, help='відрізки "тривалість:інтенсивність" через кому"')
    a = p.parse_args()
    if a.label is None:
        a.label = "step_noop" if a.path == "/noop" else f"k8s_n{a.instances}"
    run(a)


if __name__ == "__main__":
    mp.freeze_support()
    main()