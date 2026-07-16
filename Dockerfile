FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PROXY_BRIDGE_DOCKER=1 \
    PROXY_BRIDGE_HOST=0.0.0.0 \
    PROXY_BRIDGE_PORT=8787 \
    PROXY_BRIDGE_WORKDIR=/data \
    PROXY_BRIDGE_PROXY_PORT_START=21080 \
    PROXY_BRIDGE_PROXY_PORT_END=21179

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 proxybridge \
    && useradd --uid 10001 --gid proxybridge --create-home --shell /usr/sbin/nologin proxybridge

WORKDIR /app

COPY docker/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=proxybridge:proxybridge app.py webdav_sync.py ./
COPY --chown=proxybridge:proxybridge static ./static

RUN mkdir -p /data && chown proxybridge:proxybridge /data

USER proxybridge
VOLUME ["/data"]
EXPOSE 8787
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; p=os.environ.get('PROXY_BRIDGE_PORT','8787'); r=urllib.request.Request('http://127.0.0.1:'+p+'/api/status', method='HEAD', headers={'Host':'127.0.0.1:'+p}); urllib.request.urlopen(r, timeout=3).close()"]

CMD ["python", "app.py"]
