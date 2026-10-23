FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin \
    SCHULBLICK_DB=/data/schulblick.db \
    SCHULBLICK_CONFIG=/config/family.toml \
    SCHULBLICK_SPH_SECRET=/run/secrets/sph_credentials

RUN apt-get update && apt-get install -y --no-install-recommends tzdata sqlite3 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 schulblick && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin schulblick \
 && mkdir -p /data /config && chown 10001:10001 /data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-compile . && rm -rf /app/src

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1
CMD ["schulblick", "web"]
