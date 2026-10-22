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

# remote_browser.py 仅包含主服务访问内部 Browser Broker 所需的受限客户端；
# browser_fingerprint.py 是指纹浏览器配置校验、Cookie 格式与本机 CDP 客户端模块。
COPY --chown=proxybridge:proxybridge app.py webdav_sync.py remote_browser.py browser_fingerprint.py ./
# VERSION 是基础版本唯一来源；解析模块允许发布构建通过环境变量覆盖完整版本。
COPY --chown=proxybridge:proxybridge VERSION proxybridge_version.py ./
# 软件更新模块：Docker 中只用于检查并提示新版本，不会自动安装。
COPY --chown=proxybridge:proxybridge app_update.py app_payload.py ./
COPY --chown=proxybridge:proxybridge static ./static

RUN mkdir -p /data && chown proxybridge:proxybridge /data

USER proxybridge
VOLUME ["/data"]
EXPOSE 8787
STOPSIGNAL SIGTERM

# 本机健康检查忽略 HTTP_PROXY，避免容器环境代理影响 localhost 就绪判断。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; p=os.environ.get('PROXY_BRIDGE_PORT','8787'); r=urllib.request.Request('http://127.0.0.1:'+p+'/api/status', method='HEAD', headers={'Host':'127.0.0.1:'+p}); urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=3).close()"]

CMD ["python", "app.py"]
