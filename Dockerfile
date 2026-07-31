ARG PYTHON_IMAGE=python:3.13.5-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock.txt ./requirements.lock.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    -r requirements.lock.txt

RUN groupadd --gid 10001 okx \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /app --shell /usr/sbin/nologin okx
COPY --chown=10001:10001 . /app
RUN mkdir -p runtime research/cache research/results findings \
    && chown -R 10001:10001 runtime research/cache research/results findings

USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py", "run"]
