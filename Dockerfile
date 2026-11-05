FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && pip uninstall -y pip setuptools wheel

FROM python:3.12-slim AS final

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip uninstall -y pip setuptools wheel || true

WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=4s --start-period=5s --retries=3 \
    CMD ["python", "/srv/app/healthcheck.py"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
