FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY worker/requirements.txt .
RUN pip install -r requirements.txt \
    && pip uninstall -y pip setuptools wheel

FROM python:3.12-slim AS final
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN python -m pip uninstall -y pip setuptools wheel || true
WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY worker/worker.py .
EXPOSE 9100
CMD ["python", "worker.py"]