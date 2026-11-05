# Лабораторна робота 1 — CRUD-сервіс /lab-results

Варіант 4: результат лабораторного аналізу. Сервіс на Python 3.12 + FastAPI, база PostgreSQL 16, балансувальник nginx, усе в Docker Compose.

##  Звіти

| Лабораторна | Тема                                                 | Звіт                                                     |
|-------------|------------------------------------------------------|----------------------------------------------------------|
| **ЛР1** | CRUD-сервіс `/lab-results`                           | [**reports/lab1/report.md**](reports/lab1/report.md)     |
| **ЛР2** | Метрики сервісу і межа масштабованості               | [**reports/lab2/report_2.md**](reports/lab2/report_2.md) |
| **ЛР3** | Сервіс у Kubernetus з автомаштабуванням та без нього | [**reports/lab3/report_3.md**](reports/lab3/report_3.md) |

---

## Запуск

Потрібен Docker Desktop.

```
copy .env.example .env
docker compose up -d --build
```


Стенд піднімається на http://localhost:8000. Схема бази накочується автоматично з `db/schema.sql`.

Перевірка:

- http://localhost:8000/healthz — відповідь 200, якщо база доступна (503, якщо ні);
- http://localhost:8000/docs — документація API;
- `docker compose ps` — db і web у стані healthy.

## Два вебінстанси

```
docker compose up -d --scale web=2
docker compose up -d --force-recreate --no-deps lb
```

Друга команда перестворює балансувальник, щоб він отримав адреси обох інстансів.

## Навантаження

```
python loadgen\loadgen.py seed
python loadgen\loadgen.py check
python loadgen\loadgen.py series --instances 1
```

Сирі результати прогонів лежать у `results/raw`.

## Зупинка

```
docker compose down     
docker compose down -v 
```

## Структура

- `app/` — код сервісу (`main.py`) і скрипт healthcheck (`healthcheck.py`), ЛР1–ЛР3
- `db/` — схема таблиці (`schema.sql`), ЛР1
- `lb/` — конфігурація балансувальника (`nginx.conf`), ЛР1–ЛР2
- `loadgen/` — генератор навантаження: `loadgen.py` (ЛР1), `stepload.py` (ЛР2–ЛР3),
- `prometheus/` — конфігурація збору метрик (`prometheus.yml`), ЛР2–ЛР3
- `monitoring/` — values для Helm: `kps-values.yaml`, `postgres-exporter-values.yaml`, ЛР3
- `k8s/` — маніфести сервісу в Kubernetes (`postgres.yaml`, `web.yaml`), ЛР3
- `k8s-extra/` — `hpa.yaml`, `ingress.yaml`, `traefik-values.yaml`, ЛР3
- `kind/` — конфігурація кластера (`kind-config.yaml`), ЛР3
- `scripts/` — допоміжні скрипти:
  - ЛР1: `cycle_check.py` (перевірка циклу CRUD)
  - ЛР2: `usl_fit.py`, `p99_compare.py`, `m4_plot.py`, `plot_step.py`, `breakdown.py`, `recompute_achieved.py`, `sample_stats.ps1`
  - ЛР3: `usl_fit_k8s.py`, `k8s_stats.py`, `pod_rps_chart.py`, `elasticity.py`, `deploy.ps1`
- `results/` — результати вимірювань
- `reports/` — звіти з зображеннями (`lab1/`, `lab2/`, `lab3/`)
- `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, `requirements.txt`