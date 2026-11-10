# Лабораторна робота 4. Синхронізація пула воркерів

Черга задач у PostgreSQL і пул воркерів над сервісом `/lab-results`.
Python 3.12 · FastAPI · PostgreSQL 16 · Kubernetes (kind) · Prometheus

> **Варіант 4**: q_lo / q_hi = 0,02 / 0,98 · T_poll = 100 мс · L = 10 с · R = 4 · сценарій відмови Ф2 · додаткова вимога Д6

## Звіт

[**report/lab4/report.md**](reports/lab4/report.md): зображення лежать у `report/lab4/images/`.

## Запуск

Потрібні Docker Desktop, kind, kubectl і Helm.

```
copy .env.example .env
scripts\deploy.ps1
```

Скрипт створює кластер, збирає образи `web` і `worker`, завантажує їх у kind, застосовує маніфести й ставить моніторинг.

| Що | Адреса |
|---|---|
| Сервіс | http://localhost:8080 |
| Prometheus | http://localhost:9090 |

Видалення кластера:

```
kind delete cluster --name lab3
```

## Вимірювання

Задачі створює `loadgen\taskload.py`:

```
python loadgen\taskload.py --url http://localhost:8080 --rate 10 --count 20
```

Скрипти кроків лежать у `scripts/`, результати зберігаються в `results/`. Команди кожного кроку наведено у звіті.

| Крок | Тема | Скрипт |
|---|---|---|
| 4 | `FOR UPDATE SKIP LOCKED` | `step4_probe.py` |
| 5 | lease, heartbeat, fencing token | `step5_probe.py` |
| 6 | пропускна здатність X(W), σ, κ | `step6_run.py`, `step6_fit.py` |
| 7 | закон Літтла для черги | `step7_run.py` |
| 8 | коректна зупинка за SIGTERM | `step8_run.py` |
| 9 | падіння воркерів і сценарій Ф2 | `step9_run.py` |
| 10 | скасування задач (Д6) | `step10_run.py` |

## Структура

```
.
├── app/            сервіс web
├── worker/         воркер
├── db/             схема бази
├── k8s/            маніфести бази, web і воркера
├── kind/           конфігурація кластера
├── monitoring/     values для Helm
├── loadgen/        генератор задач
├── scripts/        розгортання і вимірювання
├── results/        результати вимірювань
├── reports/lab4/   звіт і зображення
└── Dockerfile, requirements.txt, .env.example
```
