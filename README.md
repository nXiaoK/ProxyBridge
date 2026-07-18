# Proxy Bridge

本地订阅节点到多协议代理端口的工具。提供 macOS 菜单栏 App，以及可在 macOS / Linux / Windows 和 Docker 运行的无壳 Web 控制台。

## 功能

- 导入订阅 URL 或原始订阅内容。
- 解析 `VLESS`、`VMess`、`Shadowsocks/SS2022`、`Trojan`、`Hysteria2/HY2`、`TUIC`、`SOCKS5`、`NaiveProxy`、`AnyTLS` 分享链接。
- 尝试解析 Clash/Mihomo YAML 中的 `proxies`。
- 为节点生成本地代理端口,同一端口同时支持 SOCKS5 与 HTTP/HTTPS CONNECT；默认仅本机可用，也可在设置中按需共享给局域网设备。
- HTTP 代理模式支持普通 HTTP 请求和 HTTPS CONNECT,兼容不支持 SOCKS5 的软件。
- 节点可选择 `sing-box` 或 `xray`；双核心兼容节点可记忆偏好，并在首选核心启动失败时按设置尝试备用核心。
- 菜单栏 App 默认不内置 `sing-box` / `xray`，首次运行时会自动下载到用户数据目录;也可构建时选择内置核心。
- 桌面/普通无壳模式下，节点操作列可启动**独立指纹浏览器**(每节点独立指纹 + 独立 Profile),并自动绑定该节点的本地代理；Docker 模式会完整关闭该能力。
- 测速结果显示年龄与过期状态，支持延迟/成功率排序、探测目标轮换，以及 3–5 路有界并发的可取消批量测速。
- 订阅可配置周期自动刷新；请求使用 `ETag` / `Last-Modified`，失败时保留旧节点并指数退避，成功后报告节点增删改 diff。
- 订阅页支持一次刷新全部 URL 订阅，任务可取消并显示逐项结果；每个订阅保留最近 20 次成功、304、失败或取消历史。取消会停止调度后续订阅，已经发出的当前请求会完成并保留真实结果。
- 默认只允许订阅 URL 解析到公网地址；使用 TUN fake-IP（如 `198.18.0.0/15`）或可信局域网订阅时，可在订阅页显式开启“允许非公网订阅地址”。
- 局域网代理支持 Basic 或 Token 认证、IP/CIDR 白名单、连接数上限、客户端列表与一键断开。
- 智能代理池支持延迟/健康策略、健康评分、熔断自动恢复和单节点临时排除。
- 智能网关提供固定 SOCKS5/HTTP 入口（默认 `21000`），按健康、延迟、轮询或随机策略从就绪代理池选择节点；隧道建立后不迁移。
- 日志中心支持服务端分页、总数、CSV/JSON 导出、按日/大小轮转、保留周期和磁盘配额。
- 诊断中心检查核心、端口、数据目录、GitHub、WebDAV、代理出口及 DNS/WebRTC 风险；联网项目默认关闭并明确列出目标。

## 运行要求

### macOS 菜单栏 App
- Apple Silicon Mac，macOS 13+。
- 菜单栏 App 不再要求目标 Mac 预装 `python3`；首次运行会自动下载独立 Python 运行时。
- 默认轻量 App 首次运行需要能访问 GitHub,用于自动下载 Python 运行时和 `sing-box` / `xray` 代理核心。

### 无壳跨平台 Web 控制台
- Python 3.10+
- macOS / Linux / Windows
- 首次运行需要能访问 GitHub，用于自动下载 `sing-box` / `xray`
- 指纹浏览器一键下载依赖上游是否发布对应平台包；也可手动设置 `PROXY_BRIDGE_FP_CHROMIUM`

### Docker
- Docker Engine 24+ / Docker Compose v2（推荐）
- 当前支持 `linux/amd64` 与 `linux/arm64`
- Web 控制台与代理端口通过宿主机回环地址访问
- 不安装或启用指纹浏览器；WebDAV 仅同步订阅 / 节点

## 文档入口

安装、部署、排障与开发资料：

- [Windows / Linux 安装与使用教程](docs/windows-linux-install.md)
- [Docker 运行教程](docs/docker.md)
- [开发文档：架构、API、测试与扩展](doc/development-guide.md)

快速选择：

| 场景 | 推荐方式 |
|---|---|
| Windows 日常使用 | 构建/获取 `ProxyBridge.exe` 后双击 |
| Linux 桌面 | `scripts/run_tray.sh` 托盘壳 |
| Linux 服务器 / 无 GUI | `python3 app.py` 无壳控制台 |
| Docker / NAS / 长期服务 | `docker compose up -d --build` |

## 构建机准备

默认构建轻量 App，不会把 `sing-box` / `xray` 打进 App 包里：

```bash
scripts/build_menubar_app.sh
```

构建脚本会按 `scripts/requirements.lock` 将锁定版本的 PyYAML 放入 App，用于解析 Clash/Mihomo YAML 订阅。默认从 Python 包索引下载；离线构建可准备 wheelhouse，并通过以下环境变量指定：

```bash
PROXY_BRIDGE_VENDOR_WHEELHOUSE=/path/to/wheelhouse \
scripts/build_menubar_app.sh
```

如需指定用于安装 vendored 依赖的构建机 Python，可设置 `PYTHON_FOR_VENDOR=/path/to/python3`。依赖安装启用哈希校验，wheelhouse 中的文件必须与锁文件一致。

如果希望构建离线版/内置核心版，携带 `--bundle-cores` 参数；此时会从构建机复制 `sing-box` / `xray` 到 App 内部，因此构建机需要先安装：

```bash
brew install sing-box
brew install xray
```

也可以通过环境变量指定二进制和 Xray 资源路径：

```bash
SING_BOX_BIN=/path/to/sing-box \
XRAY_BIN=/path/to/xray \
scripts/build_menubar_app.sh --bundle-cores
```

## 菜单栏 App

构建好的 App 位于：

```text
outputs/ProxyBridgeMenuBar.app
```

双击后会在菜单栏显示 `PB`：

- 左键点击 `PB`：打开内置控制台面板。
- 右键点击 `PB`：打开快捷菜单。
- 控制台里可以导入订阅、筛选节点、启动并复制 SOCKS5 或 HTTP/HTTPS 代理地址、停止端口。
- 节点操作列的 `浏览器` 按钮会用**指纹内核**(fingerprint-chromium)创建/复用该节点专属 Profile,底层伪造整套浏览器指纹,并绑定该节点本地代理。若节点还未启动本地代理,会先自动启动;若指纹内核缺失,会提示先下载。
- 每个节点都有 `测试` / `重测`，会临时启动该节点并通过 SOCKS5 访问设置中的探测目标，显示通信状态、延迟、成功率、结果年龄与过期状态。
- `测试可见节点` 会按当前筛选结果使用 3–5 路有界并发批量测试，支持逐项结果、失败重试和取消；单次上限 500 个节点。
- 控制台底部的 `连接日志` 会显示通过本地代理的 CONNECT/HTTP 目标、客户端本地端口、使用节点、代理协议、上下行流量、耗时和成功/失败状态。
- 运行中的每个本地代理卡片会显示累计上行、下行和合计流量,并分别展示 SOCKS5 URL 与 HTTP/HTTPS 代理 URL。
- 连接日志支持关键词搜索、按节点/状态筛选、分页，以及 CSV/JSON 导出。
- 控制台侧边栏「通用设置」可开启**局域网共享**；开关会立即重绑正在运行的代理端口，并展示局域网设备可直接使用的 SOCKS5 / HTTP 地址。

连接日志记录的是本地代理层能看到的目标域名/IP 与端口；HTTPS 通过 CONNECT 建立隧道,内容不会被解密，第一版也不会自动识别 macOS 进程名。

菜单栏版会把数据写到：

```text
~/Library/Application Support/ProxyBridge
```

浏览器 Profile 会保存在同一目录下的 `browser_profiles/`(按节点隔离,持久保留 Cookies、LocalStorage、SessionStorage);Python 运行时自动安装在 `python-runtime/`;代理核心自动安装在 `proxy-cores/`;指纹内核安装在 `fingerprint-chromium/`。方案 B 使用打过补丁的 Chromium 指纹内核(基于 Ungoogled Chromium),从 C++ 底层伪造 Canvas、WebGL、AudioContext、字体、屏幕分辨率、`hardwareConcurrency`、`deviceMemory`、Client Hints、User-Agent 等全部指纹维度:

- 每个节点从 `node_id` 确定性派生固定的 `--fingerprint` 种子——**同一节点每次启动指纹完全一致(环境可恢复),不同节点指纹互不相同(避免被网站关联为同一设备)**。
- 时区(`--timezone`)与语言(`--lang` / `--accept-lang`)按节点地区自动匹配,使浏览器时区/语言与代理出口地区一致,降低不一致被风控识别的概率。
- 使用代理时强制 `--webrtc-ip-handling-policy=disable_non_proxied_udp`，阻止非代理 UDP 候选。

指纹内核约 150–250MB,可点击控制台顶部的 `下载指纹内核` 一键安装(按本机架构从 GitHub Releases 下载并去除 Gatekeeper 隔离属性),或手动下载后用环境变量 `PROXY_BRIDGE_FP_CHROMIUM` 指向 Chromium 可执行文件。由于该内核并非每个版本都发布 macOS 包,如自动下载未命中可稍后重试或手动放置。

如果 App 是从本项目的 `outputs/` 目录启动，首次运行会尝试迁移旧 Web 版的 `work/state.json`。

## 重新构建 App

```bash
scripts/build_menubar_app.sh
```

构建脚本会自动生成图标。默认不会复制代理核心，因此包体更小；如需内置 `sing-box` 和 `xray` 到 App 的 `Contents/Resources/bridge/bin/`，使用：

```bash
scripts/build_menubar_app.sh --bundle-cores
```

如果复制到其他 Mac 后提示“无法验证开发者”或“已损坏”，可以在目标 Mac 执行：

```bash
xattr -dr com.apple.quarantine /Applications/ProxyBridgeMenuBar.app
```

## 无壳跨平台 Web 控制台（Linux / Windows / macOS）

不依赖菜单栏壳，直接启动 Python 后端 + Web 控制台，即可使用订阅导入、本地代理、测速、连接日志等核心能力。

> 完整安装/排障教程见 [`docs/windows-linux-install.md`](docs/windows-linux-install.md)。

```bash
# 默认监听 127.0.0.1:8787，数据目录为仓库下 ./work
python3 app.py

# 指定端口 / 工作目录，并自动打开系统浏览器
python3 app.py --port 8790 --workdir ~/.proxybridge --open

# Windows PowerShell 示例
python app.py --port 8790 --workdir $env:APPDATA\ProxyBridge --open
```

启动后使用终端打印的完整认证地址，例如：

```text
Proxy Bridge console: http://127.0.0.1:8787/#token=...
```

`--open` 会直接打开这个地址。页面接收令牌后会立即清除地址栏中的 fragment，并将令牌仅保存在当前标签页的会话状态中；刷新当前标签页仍可使用，另开标签页时需重新使用启动输出中的完整地址。

常用环境变量 / 参数：

| 项 | 说明 |
|---|---|
| `--port` / `PROXY_BRIDGE_PORT` | Web 控制台端口，默认 `8787` |
| `--host` / `PROXY_BRIDGE_HOST` | 控制台绑定地址，默认 `127.0.0.1` |
| `--workdir` / `PROXY_BRIDGE_WORKDIR` | 状态、日志、代理核心、浏览器 Profile 目录 |
| `--open` | 启动后用系统浏览器打开控制台 |
| `--no-core-auto-install` | 启动时不自动下载 `sing-box` / `xray` |
| `PROXY_BRIDGE_SING_BOX` / `PROXY_BRIDGE_XRAY` | 手动指定核心可执行文件 |
| `PROXY_BRIDGE_FP_CHROMIUM` | 手动指定指纹 Chromium 可执行文件 |

说明：

- 无壳模式与菜单栏 App 共用同一套 `app.py` 后端与 `static/` 前端。
- 代理核心会按当前操作系统/架构从 GitHub Releases 自动选择 Linux / Windows / macOS 包。
- 指纹浏览器若上游未提供当前平台安装包，可手动下载后通过 `PROXY_BRIDGE_FP_CHROMIUM` 指向可执行文件。
- 管理控制台默认仅本机访问；局域网共享开关只影响本地代理端口，不会把控制台暴露到局域网。
- 除 Docker 健康检查使用的 `HEAD /api/status` 外，管理 API 都需要当前后端实例令牌；直接打开不含 `#token=...` 的裸地址无法执行管理操作。

脚本调用管理 API 时，可在启动后端前设置自选的高强度 `PROXY_BRIDGE_INSTANCE_TOKEN`，并在每个请求中发送同一个令牌：

```bash
export PROXY_BRIDGE_INSTANCE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 app.py &
curl --retry 10 --retry-connrefused \
  -H "X-ProxyBridge-Instance: ${PROXY_BRIDGE_INSTANCE_TOKEN}" \
  http://127.0.0.1:8787/api/status
```

上述命令在 shell 中生成随机值，实际令牌不会写入命令历史。令牌应按密码保护，不要放入 URL query、公开日志或代码仓库。未显式设置时，后端每次启动都会生成新令牌，并只通过启动时打印的 URL fragment 交付。

## Docker 运行

Docker 模式直接提供 Web 控制台和本地代理，不包含托盘、系统浏览器调用或指纹浏览器功能。

```bash
docker compose up -d --build
docker compose logs --tail=50 proxybridge
```

从最新启动日志中复制完整的 `Proxy Bridge console:` 地址，不要只打开裸端口地址：

```text
Proxy Bridge console: http://127.0.0.1:8787/#token=...
```

Compose 默认：

- 使用 `proxybridge-data` 命名卷持久化 `/data`。
- Web 控制台默认仅发布到宿主机 `127.0.0.1:8787`；可通过 `.env` 中的 `PROXY_BRIDGE_CONSOLE_HOST=<宿主机局域网 IPv4>` 精确开放给局域网。
- 代理端口范围发布为 `127.0.0.1:21080-21179`，容器内自动分配也限制在相同范围。
- 首次启动自动下载 Linux 版 `sing-box` / `xray` 到数据卷。
- WebDAV 页面显示「仅订阅 / 节点」；上传不会删除远端已有浏览器 Profile，拉取也不会处理本地 Profile。

完整的 `docker run`、端口扩容、数据卷、离线核心、SSH 隧道和排障步骤见 [`docs/docker.md`](docs/docker.md)。

## Windows / Linux 托盘常驻壳

在无壳 Web 控制台之上，额外提供系统托盘常驻入口（打开控制台 / 重启后端 / 停止全部代理 / 退出）。

> 分系统步骤、开机自启、依赖安装见 [`docs/windows-linux-install.md`](docs/windows-linux-install.md)。

### 安装托盘依赖

```bash
python3 -m pip install -r desktop/requirements.txt
```

Windows:

```powershell
python -m pip install -r desktop/requirements.txt
```

Linux 还需桌面会话，并通常安装 AppIndicator 相关包，例如：

```bash
# Debian / Ubuntu 示例
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
```

### 启动

```bash
# Linux / macOS
scripts/run_tray.sh

# 或
python3 -m desktop.tray_host --port 18787
```

```powershell
# Windows
.\scripts\run_tray.ps1 -Port 18787

# 或
python -m desktop.tray_host --port 18787
```

托盘菜单：

- **打开控制台**：用系统浏览器打开 Web 控制台
- **重启后端**：重启 `app.py`
- **停止全部代理**：调用后端停止全部本地代理端口
- **开机自启**：登录后自动启动托盘壳（默认不自动弹控制台）
- **退出**：停止后端并退出托盘进程

默认数据目录：

| 平台 | 路径 |
|---|---|
| Windows | `%APPDATA%\ProxyBridge` |
| Linux | `$XDG_DATA_HOME/ProxyBridge` 或 `~/.local/share/ProxyBridge` |
| macOS | `~/Library/Application Support/ProxyBridge` |

说明：

- 托盘壳会拉起与无壳模式相同的 `app.py` 后端，并设置 `PROXY_BRIDGE_PARENT_PID` / `PROXY_BRIDGE_INSTANCE_TOKEN`。
- 后端日志写入工作目录下的 `tray-backend.log`。
- macOS 仍推荐使用菜单栏 App；托盘壳主要用于 Linux / Windows，也可作为 macOS 备用入口。

## Windows 双击启动（.exe）

可在 **Windows 机器**上把托盘壳打成单文件 `ProxyBridge.exe`，双击即可启动（无需先装 Python）。

> 从构建到卸载的完整 Windows 教程见 [`docs/windows-linux-install.md`](docs/windows-linux-install.md#21-方式-a双击-proxybridgeexe推荐)。

### 在 Windows 上构建

```powershell
# 需要已安装 Python 3.10+
.\scripts\build_windows_exe.ps1
```

如果 `python` 被 Windows 的 Microsoft Store 执行别名拦截，可显式指定真实解释器路径：

```powershell
.\scripts\build_windows_exe.ps1 -Python "C:\Users\<用户名>\AppData\Local\Programs\Python\Python312\python.exe"
```

构建产物：

```text
outputs\windows\release\ProxyBridge.exe
```

把 `ProxyBridge.exe` 复制到任意目录，双击启动：

1. 托盘出现 ProxyBridge 图标
2. 自动打开 Web 控制台
3. 数据与日志写入 `%APPDATA%\ProxyBridge`

说明：

- 首次运行可能从 GitHub 下载 `sing-box` / `xray`
- 未签名时 Windows SmartScreen 可能提示“未知发布者”，选择仍要运行即可
- 如需静默不自动打开浏览器，可在快捷方式目标后加 `--no-open`

### Linux 单文件二进制（可选）

```bash
scripts/build_linux_bin.sh
# 产物: outputs/linux/dist/ProxyBridge
```

### 开机自启

托盘菜单可直接切换「开机自启」。也可以命令行操作：

```bash
# 开启 / 关闭 / 查询
python3 -m desktop.tray_host --enable-autostart
python3 -m desktop.tray_host --disable-autostart
python3 -m desktop.tray_host --autostart-status
```

Windows 额外可用快捷方式脚本（桌面 + 开机启动）：

```powershell
.\scripts\create_windows_shortcuts.ps1 -ExePath .\outputs\windows\release\ProxyBridge.exe
```

实现位置：

| 平台 | 机制 |
|---|---|
| Windows | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ProxyBridge.lnk` |
| Linux | `~/.config/autostart/proxybridge.desktop` |
| macOS | `~/Library/LaunchAgents/com.proxybridge.tray.plist` |

## 局域网共享

默认情况下，所有代理端口都只监听 `127.0.0.1`，其他局域网设备无法连接。在控制台侧边栏进入 **「通用设置」**，开启 **「允许局域网设备连接本地代理」** 后：

- 外层 SOCKS5 / HTTP 代理会监听 `0.0.0.0`；默认白名单仅包含本机与 RFC1918 私有 IPv4，也可在设置中配置最多 32 条 IP/CIDR。
- 正在运行的代理会立即重新绑定，无需重启 App；切换时已有代理连接会中断并需重新连接。
- 管理控制台及管理 API 仍只监听 `127.0.0.1`，不会向局域网开放。
- 可选择无认证、用户名密码或 Token；SOCKS5 使用 RFC 1929 用户名密码认证，HTTP 代理接受 Basic/Bearer 凭据。
- 可设置全局活动连接上限；设置页显示当前客户端、来源 IP、代理和连接时长，并支持一键断开。

关闭开关后，运行中的代理会立即恢复为仅本机监听。

## WebDAV 同步

桌面/普通无壳模式支持同步**订阅 / 节点**与**指纹浏览器 Profile**（Cookie、LocalStorage 等），用于换机接力。

Docker / 禁用浏览器模式会自动切换为**仅订阅 / 节点同步**：不会打包、下载或删除 Profile；如果相同远端目录中已有桌面版浏览器数据，Docker 上传会验证并保留它们。

控制台侧边栏 **「WebDAV 同步」**：

1. 填写 WebDAV URL（如坚果云 / Nextcloud / 群晖）、用户名密码与远端目录（默认 `ProxyBridge`）。
2. 勾选「启用同步」并保存。
3. **上传到远端**：打包已停止浏览器的 Profile + 订阅状态。
4. **从远端拉取**：覆盖本机订阅/节点/浏览器记录，并解压 Profile。  
   若远端 `state_sha256` 与本机上次成功同步一致，则**跳过拉取**（已是最新）；需要强制覆盖时可在 API 传 `{"force": true}`。

约定：

- 运行中的浏览器**不会**被上传；请先关闭再同步。
- 不要两边同时打开同一浏览器实例。
- **删除订阅**会停止并删除关联节点的指纹浏览器记录与本地 Profile；WebDAV 已启用时会**自动全量上传**，远端 `state` 与多余 `profiles/*.tar.gz` 一并清理。
- 打开节点 **浏览器记录** 时（WebDAV 已启用）会先**自动拉取**一次远端。
- 浏览器记录支持 **标签**（如「访问 xx 的浏览器」）；保存标签会标记本地变更并尝试同步。
- 关闭浏览器窗口或点「停止」（且开启「关闭后自动上传」）会**自动上传**该 Profile（含标签）。
- 拉取时以远端为权威：本机多出来的订阅/节点/浏览器记录会被移除，对应本地 Profile 目录也会清理。
- Docker / 禁用浏览器模式下，上述覆盖与清理只适用于订阅和节点，本地浏览器记录/Profile 保持不变。
- 可选：关闭浏览器后自动上传、启动时检查远端更新、**忽略 SSL 证书**（过期/自签）。
- 可选：PBENC2 端到端加密（PBKDF2 + OpenSSL AES-256-CBC + HMAC 完整性校验）；本机需有 `openssl`。仍可读取旧 PBENC1 数据，新上传统一使用 PBENC2。
- 冲突策略为远端后写覆盖（LWW），并记录设备 ID。
- 每次上传先写入不可变的版本化 state/Profile 对象，再通过 WebDAV `MOVE` 原子切换 `manifest.json`；拉取时必须通过 manifest 中的 SHA-256 校验才会覆盖本地数据。

远端目录结构：

```text
{remote_root}/
  manifest.json
  state-{sha16}-{snapshot_id}.sync.json
  state.sync.json                         # 旧客户端兼容别名
  profiles/{instance_id}-{sha16}-{snapshot_id}.tar.gz
  profiles/{instance_id}.tar.gz          # 旧客户端兼容别名
```

## 代理池

运行中的本地代理会自动进入**代理池**（`status` 为 `running` / `starting` 且端口有效）。控制台「本地代理」页可复制链接；也可在 `state.json` 的 `settings.pool_token` 中查看密钥。

消费端只需 loopback + path token（无需控制台 Origin），例如：

```bash
# 假设 token 为 <token>，控制台端口 8787
curl -sS "http://127.0.0.1:8787/pool/<token>/list.json"
curl -sS "http://127.0.0.1:8787/pool/<token>/proxies.txt"
curl -sS "http://127.0.0.1:8787/pool/<token>/get?strategy=random&protocol=socks5"
```

说明：

- `list.json` / `proxies.txt` 返回**可发现**成员，可能短暂包含尚未就绪的 `starting` 端口。
- `/get` 默认只返回**已就绪**端口（`running` 且本地代理进程活跃）；需要包含启动中成员时可加 `?include_starting=1`。
- `proxies.txt` 默认每个成员输出 socks5 一行 + http 一行；可用 `?protocol=socks5` 或 `http` 收窄。
- `/get` 支持 `random`、`round_robin`、`health` 和 `latency` 策略；健康策略综合 EWMA 延迟、测速成功率和连续失败。
- 连续失败会打开熔断器，冷却后仅放行一次 half-open 探测；成功后自动恢复。控制台也可将单个成员临时排除最多 7 天。
- 轮换密钥后旧链接立即失效（控制台「轮换密钥」，或 `POST /api/pool/token/rotate`）。
- 代理池 HTTP 接口和管理控制台始终只监听 `127.0.0.1`；即使开启局域网共享，远端设备也只能使用设置页列出的直连代理地址。access log 会脱敏 path 中的 token。

### 智能网关

在「本地代理」页启用智能网关后，会创建一个稳定入口（默认 `21000`），客户端只需配置一次 SOCKS5 或 HTTP 地址。每个新连接根据所选策略从健康且已就绪的代理池成员中选择；连接建立后保持该节点，节点失败只影响当前连接。网关会把连接失败反馈给代理池熔断器，不重复计入普通 Bridge 流量。

网关端口必须与普通代理端口不同，并遵循当前局域网监听和认证设置。Docker Compose 当前只发布动态代理端口范围，没有独立的 `21000` 映射，因此 Docker 模式会明确禁用智能网关；如需使用，请在宿主机模式运行或自行规划并发布固定端口后再扩展配置。

## 诊断中心

控制台侧边栏 **「诊断中心」** 默认只运行本地检查：

- 读取 `sing-box` / `xray` 版本并核验可执行性。
- 检查控制台和活动代理是否真实监听，以及 Docker 代理端口是否处于发布范围。
- 检查工作目录、运行目录、状态文件的读写权限、POSIX 权限和剩余磁盘空间。
- 在当前浏览器采集 WebRTC ICE 候选；不配置第三方 STUN，无候选时只报告“可能被隐藏”，不会误判为安全。

勾选 **「包含网络检查」** 后，才会额外访问固定的 `api.github.com`、已配置的 WebDAV，以及经所选 SOCKS5 代理访问 `api.ipify.org`。WebDAV URL 不是 HTTPS 或关闭证书校验时，诊断会报告风险但不会发送账号凭据。出口 IP、LAN 地址和诊断结果不会写入状态文件。

DNS 项验证 ProxyBridge 把诊断域名交给 SOCKS5 远端解析；它无法观察操作系统或其他浏览器请求，因此只作为风险提示，不能替代系统级抓包或专用泄漏测试。

## 数据位置

Web 控制台模式：

- 状态文件：`work/state.json`
- 上一份有效状态备份：`work/state.json.bak`
- 运行配置：`work/runtime/*.json`
- 核心日志：`work/runtime/*.log`

状态保存采用临时文件 + `fsync` + 原子替换；主状态损坏或缺失时会尝试读取 `.bak`，两者都不可用时会拒绝以空状态启动，避免静默丢失数据。数据目录权限为 `0700`，状态与运行日志等敏感文件为 `0600`。

第一版会把节点参数保存在本地明文状态文件中。后续版本建议接入 macOS Keychain。

## 测试

本地离线验证与 CI 的核心命令：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node --check static/auth.js
node --check static/diagnostics.js
node --check static/app.js
node --test tests/test_frontend_auth.js tests/test_frontend_diagnostics.js
bash -n scripts/*.sh
git diff --check
```
