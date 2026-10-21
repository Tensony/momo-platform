# wallet-api image. Multi-stage: build wheels once, ship a slim non-root runtime.
FROM python:3.12-slim AS build
WORKDIR /w
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /install /usr/local
COPY src ./src
USER 10001
EXPOSE 8080
# 2 workers x 4 threads; DB_POOL_MAX is per worker process (see ecs.tf).
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "60", \
     "--access-logfile", "-", "--access-logformat", "%(h)s %(r)s %(s)s %(L)s", \
     "momo.api.app:create_app()"]
