# Docker 运行教程

Docker 模式适合 Linux 服务器、NAS、家庭服务器和不需要桌面壳的长期运行环境。默认 Compose 除 Web 控制台与代理服务外，还提供按需创建的远程指纹浏览器：其他电脑可通过带密码的 HTTPS 链接直接使用服务器上运行的浏览器。

主服务的自动核心下载与运行验证覆盖 `linux/amd64` 和 `linux/arm64`。远程浏览器所需的 fingerprint-chromium Linux 包当前只有 x86_64，Broker 因此固定创建 `linux/amd64` worker。`arm64` 宿主机只有在 Docker 已启用 amd64 仿真时才能运行这些 worker；性能会明显低于原生 amd64，且 GPU 加速通常受限或不可用。其他 CPU 架构不作为受支持配置。

完整远程浏览器模式要求 Docker Engine API **1.45+**（通常为 Docker Engine 26+）和 Docker Compose v2。Broker 使用该 API 的命名卷 `volume-subpath` 能力，只把当前实例的 Profile 和指纹内核目录挂入 worker；API 版本不足时 Broker 会拒绝启动远程浏览器。

## Docker 模式与桌面模式的区别

- 使用同一套订阅导入、节点测试、本地 SOCKS5 + HTTP 代理、连接日志和代理池功能。
- Web 控制台默认发布到宿主机 `127.0.0.1:8787`。
- 默认 Compose 通过内部 Browser Broker 为每条运行记录创建一个独立 `lscr.io/linuxserver/chromium` worker；单容器 `docker run` 未配置 Broker 时不显示浏览器入口。
- 每个实例使用独立 Profile、由记录派生的固定指纹、对应节点代理以及独立登录密码；停止 worker 不删除 Profile。
- worker 的 `/config` 是随容器删除的匿名运行卷，持久化命名卷仅挂入 `/config/browser-profile`；Selkies 的 TLS、认证和其他运行文件不会进入 Profile 或 WebDAV 快照。
- worker 访问节点代理时，主服务只对 Broker 确认的“精确 worker IP + 分配给该实例的代理端口”免除二次认证，不会放行整个 worker 子网或其他容器。
- 浏览器功能启用时，WebDAV 可同步已停止的浏览器 Profile；只有未配置 Broker 或显式禁用浏览器时才退回**仅订阅 / 节点同步**。
- 容器内代理监听由 Docker 管理；Compose 默认只把代理端口发布到宿主机回环地址。

> **Docker Socket 风险：**Browser Broker 挂载 `/var/run/docker.sock` 才能动态创建 worker。即使挂载写成 `:ro`，这只限制 Socket 文件本身，不能限制 Docker API；Broker 仍可创建/删除容器、挂载任意宿主机路径，权限事实上接近宿主机 root。请只在专用、可信的 Docker 主机部署，不要让不可信容器加入 `browser-control` 网络或读取 `proxybridge-browser-control` 卷，也不要把 Broker 端口发布到宿主机。

## 1. 使用 Docker Compose（推荐）

在仓库根目录执行：

```bash
docker compose up -d --build
```

查看状态和日志：

```bash
docker compose ps
docker compose logs --tail=50 proxybridge
docker compose logs --tail=50 browser-broker
```

需要持续跟踪日志时再使用 `docker compose logs -f proxybridge browser-broker`。

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

主服务与 Broker 的 Compose 停止宽限期均为 3 分钟，用于等待正在创建的 worker 完成回收、优雅停止现有 worker 并让 Profile 落盘。Broker 每次启动还会先清理上次异常退出遗留的 ProxyBridge worker 和临时凭证，因此重启 Broker 会终止旧远程会话，不会继续保留无人管理的公开端口。

删除容器但保留数据：

```bash
docker compose down
```

如需连同订阅、日志、已下载核心和浏览器 Profile 一起删除：

```bash
docker compose down -v
```

> `down -v` 会永久删除 `proxybridge-data` 数据卷，其中包括所有浏览器 Profile；请先确认不再需要或已经备份。

## 2. 使用 docker run

下面的单容器命令只启动 ProxyBridge 主服务，没有 Browser Broker，也不会挂载 Docker Socket，因此远程浏览器功能保持关闭。需要浏览器共享时应使用仓库提供的完整 Compose 拓扑。

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
| `127.0.0.1:23000-23099` | Browser Broker 动态分配的远程浏览器 HTTPS 端口，每个运行实例占一个 |

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

`23000-23099` 不由 Compose 的固定 `ports` 列表发布，而是在 Broker 创建 worker 时直接绑定到 `PROXY_BRIDGE_BROWSER_HOST`。Broker 从小到大选择空闲端口；默认并发上限为 10。扩大浏览器范围时应同时调整 `PROXY_BRIDGE_BROWSER_PORT_START`、`PROXY_BRIDGE_BROWSER_PORT_END` 和宿主机防火墙规则，并确保范围不与其他服务冲突。

## 4. 数据持久化

完整 Compose 必须使用两个命名卷：

```text
proxybridge-data:/data
proxybridge-browser-control:/control
```

`proxybridge-data` 包含：

- `/data/state.json`：订阅、节点、设置和运行记录
- `/data/runtime/`：核心配置与日志
- `/data/proxy-cores/`：自动下载的 `sing-box` / `xray`
- `/data/fingerprint-chromium/`：控制台首次下载的 Linux 指纹内核
- `/data/browser_profiles/`：每个远程浏览器实例的独立 Profile

Broker 通过 Docker Engine API 1.45+ 的 `volume-subpath` 从这个命名卷挂载两个经过校验的子目录：当前实例 Profile 以读写方式挂到 worker 的 `/config/browser-profile`，指纹内核以只读方式挂到 `/opt/fingerprint-chromium`。worker 自己的 `/config` 是匿名运行卷，删除容器时一并删除；自签名证书、Basic Auth 配置和其他 Selkies 运行文件不会混入持久化 Profile。

`proxybridge-browser-control` 只保存 Broker Bearer token、worker 初始化脚本和运行期间的临时密码文件。Broker 以读写方式挂载该卷，主服务只读挂载；密码目录和文件权限分别为 `0700` / `0600`，主服务只能读取单独设为只读的 token。该卷同样必须是 Docker 命名卷，不能改成宿主机 bind mount，也不能映射到 Web 根目录或其他容器。

查看卷：

```bash
docker volume inspect proxybridge-data
docker volume inspect proxybridge-browser-control
```

远程浏览器模式**不支持**把 `/data` 改成 `./docker-data:/data` 一类宿主机 bind mount。Broker 会校验主服务的 `/data` 来源并拒绝宿主目录：命名卷子路径既是 worker 隔离挂载的部署契约，也用于避免数据目录中的符号链接被解释成任意宿主机路径。需要宿主机直接管理文件时，请通过命名卷备份/恢复，而不是替换此挂载；只有明确关闭 Browser Broker 的单容器模式可自行承担 bind mount 的权限与路径安全风险。

worker 网络也属于强制部署契约：必须是 `internal=true`、作用域为 `local` 的 Docker `bridge`，配置无冲突的 RFC1918 IPv4 IPAM，并让 ProxyBridge owner 容器以配置的名称或网络别名加入。Broker 会在拉取/创建 worker 前逐项校验；这既阻止 worker 绕过节点代理直接出网，也确保代理免认证仅能按 Broker 返回的精确 worker IP 与实例端口授权。

## 5. 代理核心与 Linux 指纹内核下载

默认启动后会从 GitHub Releases 自动下载当前 Linux 架构对应的 `sing-box` 和 `xray`，并保存到 `/data/proxy-cores/`。因此首次启动需要容器能访问 GitHub。

如果 fnOS、NAS 或服务器访问 GitHub 很慢，不需要进入容器或直接修改命名卷。可在另一台电脑下载官方 Release 文件，使用最新启动日志中的完整认证 URL 打开控制台，点击侧边栏底部「本地导入」上传：

- Linux amd64 sing-box：例如 `sing-box-1.13.15-linux-amd64-glibc.tar.gz`。
- Linux amd64 Xray：例如 `Xray-linux-64.zip`。
- 远程浏览器指纹内核：例如 `fingerprint-chromium_linux_x64.zip`，具体名称以上游实际 Release 为准。

文件名不能修改，后端会按官方文件名校验 Linux 与 CPU 架构。可填写官方 SHA-256，服务端会在安装前核对；留空时也会返回实际 SHA-256。代理核心替换前需停止所有正在使用该核心的本地代理，指纹内核替换前需停止所有浏览器 worker。上传上限为 2 GiB，解压后上限为 4 GiB，只应上传可信官方来源文件。

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

远程浏览器首次使用还需要单独安装 fingerprint-chromium：

1. 使用最新启动日志中的完整认证 URL 打开控制台。
2. 点击控制台的「下载指纹内核」，或点击「本地导入」上传已下载的 Linux x86_64 完整包，等待约 150–250 MB 的文件安装完成。
3. 内核会保存到 `proxybridge-data` 卷的 `/data/fingerprint-chromium/`；不要只把一个可执行文件放到卷外，Broker 必须通过命名卷 `volume-subpath` 把包含 `chrome-wrapper` 及相邻资源的完整目录只读挂载给 worker。
4. 第一次启动浏览器时，Broker 还会拉取 `lscr.io/linuxserver/chromium:latest`，所需时间取决于镜像缓存和网络速度。

控制台下载器会按 Linux release 选择指纹内核。当前 Linux 上游包为 x86_64，因此 Compose worker 显式使用 `linux/amd64`。在 arm64 NAS/服务器上，先确认 Docker 支持 amd64 仿真；即使能启动，也应预期更高 CPU 占用、更低交互帧率，以及 GPU/硬件视频加速不可用。对交互性能有要求时使用 amd64 宿主机。

## 6. WebDAV 同步

在控制台进入「WebDAV 同步」配置服务器。默认 Compose 已启用远程浏览器，因此同步范围包含订阅、节点、浏览器记录和已停止的 Profile。

行为约定：

- 上传：发布订阅、节点、浏览器记录和已停止的 Profile；运行中的 worker Profile 不参与打包。
- 拉取：校验后覆盖本机状态并恢复 Profile；不要在两台设备同时运行同一浏览器实例。
- 如果 Broker 未配置、不可用状态未知或浏览器被显式禁用，Profile 操作会 fail-closed；只同步订阅/节点，或拒绝打包可能仍在运行的 Profile。
- 为避免旧版本客户端重新上传时破坏保留语义，混用桌面版与 Docker 版时建议所有设备升级到包含该功能的版本。

## 7. 从其他电脑访问控制台与浏览器

默认端口只发布到服务器的 `127.0.0.1`，不会直接暴露到局域网或公网。远程管理建议使用 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 user@server
```

然后在本机打开容器启动日志打印的完整认证地址。该地址仍以 `http://127.0.0.1:8787/` 开头，因此可以直接通过上述隧道使用。

如果确认局域网或 VPN 可信，也可以让控制台和远程浏览器只绑定 Docker 宿主机的具体私有 IPv4。先在仓库根目录创建 `.env`；以下三项通常应填写同一个地址：

```dotenv
# Web 控制台发布地址；只填宿主机明确的私有 IPv4，不能填 0.0.0.0。
PROXY_BRIDGE_CONSOLE_HOST=192.168.1.20
# 浏览器 worker 实际绑定地址；只允许回环或宿主机明确的私有 IPv4。
PROXY_BRIDGE_BROWSER_HOST=192.168.1.20
# 控制台生成远程浏览器链接时使用的地址；通常必须与上一项相同。
PROXY_BRIDGE_BROWSER_PUBLIC_HOST=192.168.1.20
```

把示例 IP 替换为 Docker 宿主机的实际局域网 IP，然后重新创建容器：

```bash
docker compose up -d --build --force-recreate
```

重新查看容器日志，局域网设备使用其中以 `http://192.168.1.20:8787/#token=...` 开头的完整认证地址。ProxyBridge 和 Browser Broker 只接受明确的回环或私有 IPv4，不接受 `0.0.0.0`、任意主机名或公网地址。

宿主机防火墙需要只向可信客户端网段开放：

- TCP `8787`：ProxyBridge Web 控制台。
- TCP `23000-23099`：远程浏览器 HTTPS；每个运行实例动态占用其中一个端口。

上述设置开放的是 Web 控制台与远程浏览器，不会开放节点代理端口。不需要让其他电脑直接连接节点代理时，无需开放 `21080-21179`；确有需要时再在 `.env` 增加下面的发布地址，并按同样原则限制防火墙来源：

```dotenv
# 节点 SOCKS5/HTTP 代理发布地址；不共享代理时不要设置。
PROXY_BRIDGE_PROXY_HOST=192.168.1.20
```

### 7.1 启动、打开与停止远程浏览器

1. 首次使用先按第 5 节下载 Linux 指纹内核。
2. 在节点操作列点击「浏览器」。凭据弹窗默认用户名为 `proxybridge`，前端会生成一条可复制、可修改的随机强密码；每个实例应使用不同密码。
3. 提交后，ProxyBridge 会先确保节点代理已运行，再创建独立 worker。Broker 等待 worker 的 `3001` 端口和 HTTPS 健康检查都就绪后，控制台才会自动打开 `https://<PROXY_BRIDGE_BROWSER_PUBLIC_HOST>:<动态端口>/`。
4. 若弹窗被浏览器阻止，可进入「浏览器记录」，在运行记录上点击「远程打开」。这里也会显示用户名、URL、端口和 worker 状态。
5. 使用该用户名和本次密码通过 worker 的 Basic Auth。密码只存在于本次凭据弹窗、用户记忆和运行 worker 的只读 secret 中，不写入 `state.json`，也不会由状态 API 返回；关闭后重新启动需设置新密码。
6. worker 通过内部网络访问该实例的节点 SOCKS5 端口；主服务仅对 Broker 实时返回的精确 worker IP 和该代理端口组合免除代理二次认证，另一个 worker 或同网段容器不能复用此权限。
7. 使用完后在「浏览器记录」点击「停止」。Broker 会停止并删除 worker、匿名 `/config` 运行卷与临时 secret，但保留该实例的独立 Profile、固定指纹种子和节点关联，便于下次继续。

每次 worker 容器启动时都会在自己的匿名 `/config` 运行卷中动态生成自签名 HTTPS 证书，所以浏览器访问新 worker 时会提示证书不受信任。该匿名卷不会参与 WebDAV，并随 worker 删除。这只能避免链路明文传输，不能提供受信任的服务器身份。内置 Basic Auth 只是可信 LAN/VPN 内的便利防护，不是公网级访问控制。

**不要把 `8787` 或 `23000-23099` 直接暴露到公网。**确需公网使用时，应继续让 worker 绑定回环/私有地址，在前面部署使用受信任证书的 TLS 反向代理，并增加 SSO、MFA 或等效强认证；还应限制源地址、速率和会话时长。反向代理连接自签名上游时，应显式信任对应证书或将跳过上游校验严格限制在本机链路，不能关闭客户端到公网入口的 TLS 校验。

管理 API 使用每次启动生成的实例令牌，主要用于阻止本机其他网页或进程误调用；它不是多用户远程登录系统。局域网代理可在「通用设置」中启用 Basic/Token 认证、IP/CIDR 白名单和连接上限。建议给宿主机设置 DHCP 静态租约，并用防火墙限制只允许可信网段访问；安全要求更高时优先使用 VPN 或 SSH 隧道。

「诊断中心」在 Docker 中会核验控制台监听和 `21080-21179` 发布范围。GitHub、WebDAV、代理出口等联网检查默认关闭；启用前请确认页面列出的固定目标。远程浏览器的 worker 状态、URL 和端口可在「浏览器记录」中检查。

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

查看日志中的 GitHub 下载错误，确认容器 DNS、HTTPS 出站和系统时间正常。若 GitHub 在当前网络不可用，在其他电脑下载保留官方文件名的 Linux/当前架构发布包，通过控制台「本地导入」安装；成功后同样会持久化在数据卷中。

### 点击浏览器提示未找到 Linux 指纹内核

先在控制台点击「下载指纹内核」，或本地导入 Linux x86_64 完整发布包，并确认 `/data/fingerprint-chromium/` 位于 `proxybridge-data` 卷内。Broker 需要完整的 Linux 发行目录和可执行 `chrome-wrapper`，上传单个 `chrome`、`.7z` 或只设置指向卷外文件的路径都无法供 worker 使用。

### 一直显示“正在创建远程浏览器 Worker”，随后提示无法连接 Broker

Broker 可以正常显示“已连接”但首次启动仍可能耗时很久，因为 `lscr.io/linuxserver/chromium:latest` 不会在构建 ProxyBridge 时下载，而是在第一次创建 Worker 前按 `linux/amd64` 拉取。先检查 Broker 日志和本地镜像：

```bash
docker compose ps
docker compose logs --tail=200 browser-broker
docker image inspect lscr.io/linuxserver/chromium:latest --format '{{.Os}}/{{.Architecture}}'
```

若镜像不存在，可在 fnOS Docker 宿主机上直接拉取并观察实际进度；拉取完成后无需重建 ProxyBridge，回到控制台重新启动该浏览器记录即可：

```bash
docker pull --platform linux/amd64 lscr.io/linuxserver/chromium:latest
docker image inspect lscr.io/linuxserver/chromium:latest --format '{{.Os}}/{{.Architecture}}'
```

如果 fnOS 无法稳定访问 `lscr.io`，可在网络正常且支持 Docker 的电脑预先拉取并导出：

```bash
docker pull --platform linux/amd64 lscr.io/linuxserver/chromium:latest
docker image inspect --platform linux/amd64 \
  lscr.io/linuxserver/chromium:latest \
  --format '{{.Os}}/{{.Architecture}}'
docker image save --platform linux/amd64 \
  -o proxybridge-chromium-amd64.tar \
  lscr.io/linuxserver/chromium:latest
```

OrbStack、启用 containerd image store 的 Docker Desktop 等环境中，镜像标签可能指向多平台 manifest index，普通 `docker image inspect` 因而只显示 `/`，普通 `docker save` 也可能尝试导出未缓存的平台并报 `content digest ... not found`。这不代表 amd64 镜像拉取失败；上面的 `inspect --platform` 与 `save --platform` 会明确选择已经缓存的 `linux/amd64` 子镜像。

将 `proxybridge-chromium-amd64.tar` 复制到 fnOS 后导入：

```bash
docker load -i proxybridge-chromium-amd64.tar
docker image inspect lscr.io/linuxserver/chromium:latest --format '{{.Os}}/{{.Architecture}}'
```

最后一条必须输出 `linux/amd64`。本地导入 `sing-box`、`Xray` 或 fingerprint-chromium 不能替代这个 Worker Docker 镜像；两者是不同的运行层。

### 远程浏览器链接打不开

确认 `.env` 的 `PROXY_BRIDGE_BROWSER_HOST` 与 `PROXY_BRIDGE_BROWSER_PUBLIC_HOST` 都是宿主机实际私有 IPv4，重新创建过 Compose 服务，并检查 worker 是否占用了 `23000-23099` 内的端口：

```bash
docker ps --filter label=com.proxybridge.browser-worker=1
```

再检查防火墙是否允许当前可信客户端访问该端口。证书警告是每个 worker 动态自签名证书的预期行为；若 Basic Auth 失败，请停止并重新启动实例，使用新弹窗中生成或填写的密码。

### arm64 上浏览器很慢或无法启动

远程浏览器 worker 当前为 `linux/amd64`，arm64 依赖 Docker 仿真。确认宿主机已启用对应 binfmt/QEMU 支持；若已能启动但交互缓慢、CPU 占用高或没有 GPU 加速，属于当前架构限制，建议迁移到 amd64 Docker 主机。

### 数据目录 Permission denied

确认 Compose 仍使用默认的 `proxybridge-data:/data` 命名卷，并通过 `docker volume inspect proxybridge-data` 检查卷是否存在；远程浏览器模式不要改成宿主机 bind mount。若从旧的绑定目录迁移，请先备份，再把数据恢复到命名卷并保留镜像内 UID/GID `10001` 的访问权限。不要用宽泛的 `chmod 777` 作为长期方案，因为状态文件和浏览器 Profile 都包含敏感信息。
