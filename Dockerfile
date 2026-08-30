ARG PYTHON_IMAGE=python:3.13.5-slim-bookworm
ARG ALPACA_DEPLOYMENT_COMMIT=unknown
FROM ${PYTHON_IMAGE}

ARG ALPACA_DEPLOYMENT_COMMIT

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ALPACA_BUILD_COMMIT=${ALPACA_DEPLOYMENT_COMMIT} \
    TZ=UTC

LABEL org.opencontainers.image.revision=${ALPACA_DEPLOYMENT_COMMIT}

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock.txt ./requirements.lock.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    -r requirements.lock.txt

RUN groupadd --gid 10001 alpaca \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /app --shell /usr/sbin/nologin alpaca
COPY --chown=10001:10001 . /app
RUN mkdir -p runtime research/cache research/results/edges \
    && chown -R 10001:10001 runtime research/cache research/results

USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py", "run"]
