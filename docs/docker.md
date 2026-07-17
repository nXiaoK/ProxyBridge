# Docker 运行教程

Docker 模式适合 Linux 服务器、NAS、家庭服务器和不需要桌面壳的长期运行环境。容器直接提供现有 Web 控制台，不安装托盘或浏览器内核。

当前自动核心下载与运行验证覆盖 `linux/amd64` 和 `linux/arm64`。其他 CPU 架构可能能构建基础镜像，但 ProxyBridge 会按 x86_64 选择核心资产，因此不作为受支持配置。

## Docker 模式与桌面模式的区别

- 使用同一套订阅导入、节点测试、本地 SOCKS5 + HTTP 代理、连接日志和代理池功能。
- Web 控制台默认发布到宿主机 `127.0.0.1:8787`。
- **不提供指纹浏览器功能**：控制台不会显示浏览器按钮或指纹内核下载入口，浏览器 API 也会拒绝调用。
- WebDAV 自动切换为**仅订阅 / 节点同步**：不会上传、下载或删除浏览器 Profile；若远端已有桌面版浏览器数据，Docker 上传会保留它们。
- 容器内代理监听由 Docker 管理；Compose 默认只把代理端口发布到宿主机回环地址。

## 1. 使用 Docker Compose（推荐）

在仓库根目录执行：

```bash
docker compose up -d --build
```

查看状态和日志：

```bash
docker compose ps
docker compose logs --tail=50 proxybridge
```

需要持续跟踪日志时再使用 `docker compose logs -f proxybridge`。

从最新启动日志中找到 `Proxy Bridge console:`，在浏览器打开其完整地址：

```text
Proxy Bridge console: http://127.0.0.1:8787/#token=...
```

`#token=...` 是当前容器进程的管理令牌。页面接收后会清除地址栏 fragment，并将令牌仅保存在当前标签页的会话状态中。容器重启后令牌会更新，请重新查看最新日志；不要只打开不带 fragment 的裸地址。

停止或重新启动：

```bash
docker compose stop
docker compose start
```

删除容器但保留数据：

```bash
docker compose down
```

如需连同订阅、日志和已下载核心一起删除：

```bash
docker compose down -v
```

> `down -v` 会永久删除 `proxybridge-data` 数据卷，请先确认不再需要其中的数据。

## 2. 使用 docker run

先构建镜像：

```bash
docker build -t proxybridge:local .
```

再启动：

```bash
docker run -d \
  --name proxybridge \
  --init \
  --restart unless-stopped \
  --read-only \
  --tmpfs /tmp:size=256m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -p 127.0.0.1:8787:8787 \
  -p 127.0.0.1:21080-21179:21080-21179 \
  -v proxybridge-data:/data \
  proxybridge:local
```

查看日志：

```bash
docker logs --tail=50 proxybridge
```

## 3. 端口说明

默认发布两类端口：

| 端口 | 用途 |
|---|---|
| `127.0.0.1:8787` | Web 控制台、管理 API、代理池 URL |
| `127.0.0.1:21080-21179` | 节点启动后生成的本地 SOCKS5 + HTTP 代理端口 |

同一个代理端口同时支持：

```text
socks5://127.0.0.1:21080
http://127.0.0.1:21080
```

Docker 模式会把自动分配限制在已发布的 `21080-21179` 范围内。默认最多可同时映射 100 个不同代理端口；需要更多时，同时修改：

> 智能网关固定入口当前不在 Compose 的发布范围内。Docker 模式会拒绝启用智能网关，避免出现容器内显示运行但宿主机无法连接的假状态。

1. `PROXY_BRIDGE_PROXY_PORT_START` / `PROXY_BRIDGE_PROXY_PORT_END`
2. Compose 或 `docker run -p` 的端口范围

例如扩展到 `21279`：

```yaml
environment:
  PROXY_BRIDGE_PROXY_PORT_START: "21080"
  PROXY_BRIDGE_PROXY_PORT_END: "21279"
ports:
  - "127.0.0.1:21080-21279:21080-21279"
```

不要只改环境变量而不改端口映射，否则控制台可能显示已启动，但宿主机无法连接超出映射范围的端口。

## 4. 数据持久化

Compose 使用命名卷：

```text
proxybridge-data:/data
```

其中包含：

- `/data/state.json`：订阅、节点、设置和运行记录
- `/data/runtime/`：核心配置与日志
- `/data/proxy-cores/`：自动下载的 `sing-box` / `xray`

查看卷：

```bash
docker volume inspect proxybridge-data
```

也可改成宿主机目录：

```yaml
volumes:
  - ./docker-data:/data
```

镜像内进程使用 UID/GID `10001`。绑定宿主机目录前请确保可写，例如 Linux：

```bash
sudo mkdir -p ./docker-data
sudo chown -R 10001:10001 ./docker-data
```

## 5. 代理核心下载

默认启动后会从 GitHub Releases 自动下载当前 Linux 架构对应的 `sing-box` 和 `xray`，并保存到 `/data/proxy-cores/`。因此首次启动需要容器能访问 GitHub。

若要禁止自动下载，在 Compose 服务中增加：

```yaml
command: ["python", "app.py", "--no-core-auto-install"]
```

此时需自行把核心放入 `/data/proxy-cores/bin/`，或设置：

```yaml
environment:
  PROXY_BRIDGE_SING_BOX: /data/custom/sing-box
  PROXY_BRIDGE_XRAY: /data/custom/xray
```

并把对应文件挂载进容器、赋予可执行权限。

## 6. WebDAV 同步

在控制台进入「WebDAV 同步」配置服务器。Docker 模式页面会显示同步范围为「仅订阅 / 节点」。

行为约定：

- 上传：发布本机订阅与节点，不读取本地浏览器目录。
- 若远端已有桌面版浏览器记录/Profile，上传会验证并保留它们，不执行 Profile 垃圾回收。
- 拉取：覆盖本机订阅与节点，但不下载 Profile，也不覆盖或删除本地浏览器数据。
- 为避免旧版本客户端重新上传时破坏保留语义，混用桌面版与 Docker 版时建议所有设备升级到包含该功能的版本。

## 7. 从其他电脑访问控制台

默认端口只发布到服务器的 `127.0.0.1`，不会直接暴露到局域网或公网。远程管理建议使用 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 user@server
```

然后在本机打开容器启动日志打印的完整认证地址。该地址仍以 `http://127.0.0.1:8787/` 开头，因此可以直接通过上述隧道使用。

如果确认局域网可信，也可以让控制台只绑定 Docker 宿主机的具体局域网 IPv4。先在仓库根目录创建 `.env`：

```dotenv
PROXY_BRIDGE_CONSOLE_HOST=192.168.1.20
```

把示例 IP 替换为 Docker 宿主机的实际局域网 IP，然后重新创建容器：

```bash
docker compose up -d --build --force-recreate
```

重新查看容器日志，局域网设备使用其中以 `http://192.168.1.20:8787/#token=...` 开头的完整认证地址。ProxyBridge 只信任 `.env` 中这个精确的 RFC1918 IPv4，不接受 `0.0.0.0`、其他局域网 IP、任意主机名或公网地址。

上述设置只开放 Web 控制台。若还需要让局域网设备连接代理端口，可在 `.env` 中额外设置：

```dotenv
PROXY_BRIDGE_PROXY_HOST=192.168.1.20
```

管理 API 使用每次启动生成的实例令牌，主要用于阻止本机其他网页或进程误调用；它不是多用户远程登录系统。局域网代理可在「通用设置」中启用 Basic/Token 认证、IP/CIDR 白名单和连接上限，但控制台与代理端口仍不应暴露到公网。建议给宿主机设置 DHCP 静态租约，并用防火墙限制只允许可信局域网网段访问；安全要求更高时仍优先使用 SSH 隧道或带认证的反向代理。

「诊断中心」在 Docker 中会核验控制台监听和 `21080-21179` 发布范围。GitHub、WebDAV、代理出口等联网检查默认关闭；启用前请确认页面列出的固定目标。Docker 模式不提供指纹浏览器，因此 WebRTC 项只反映当前打开控制台的浏览器。

自动化调用 `/api/*` 时，必须从启动 URL 取得令牌，并作为 `X-ProxyBridge-Instance` 请求头发送。唯一例外是供 Docker 使用的无响应体健康检查 `HEAD /api/status`。令牌应按密码保护，尤其不要把包含 fragment 的启动日志公开。

## 8. 更新镜像

拉取新代码后：

```bash
docker compose build --pull
docker compose up -d
```

命名卷中的数据和代理核心会保留。

## 9. 常见问题

### 控制台打不开

```bash
docker compose ps
docker compose logs --tail=200 proxybridge
```

确认端口映射仍为 `127.0.0.1:8787:8787`，然后执行 `docker compose logs --tail=50 proxybridge`，从同一台宿主机打开最新的完整认证地址。

### 访问 8787 返回 403 Forbidden

控制台会同时校验实例令牌、请求 `Host` 和 `Origin`。默认使用日志中以 `http://127.0.0.1:8787/` 开头的完整认证地址，不要使用 `http://0.0.0.0:8787` 或未经配置的反向代理域名。

若页面能打开但提示管理 API 凭证无效，请关闭该标签页，并重新使用最新启动日志中的完整地址。若使用服务器局域网 IP，请确认 `.env` 中 `PROXY_BRIDGE_CONSOLE_HOST` 与浏览器地址完全一致，并执行过 `docker compose up -d --force-recreate`；IP 变化后也要更新 `.env` 并重新创建容器。

### 节点启动了但代理连不上

确认实际端口位于 `21080-21179`，并检查 Compose 是否发布了完整同名范围：

```bash
docker port proxybridge
```

### 首次启动后核心仍显示 missing

查看日志中的 GitHub 下载错误，确认容器 DNS、HTTPS 出站和系统时间正常。核心下载成功后会持久化在数据卷中。

### 数据目录 Permission denied

使用命名卷，或把绑定目录所有者调整为 `10001:10001`。不要用宽泛的 `chmod 777` 作为长期方案，因为状态文件包含订阅和节点敏感信息。
