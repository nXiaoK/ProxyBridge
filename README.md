# Proxy Bridge

本地订阅节点到多协议代理端口的工具。提供 macOS 菜单栏 App，以及可在 macOS / Linux / Windows 运行的无壳 Web 控制台。

## 功能

- 导入订阅 URL 或原始订阅内容。
- 解析 `VLESS`、`VMess`、`Shadowsocks/SS2022`、`Trojan`、`Hysteria2/HY2`、`TUIC`、`SOCKS5`、`NaiveProxy`、`AnyTLS` 分享链接。
- 尝试解析 Clash/Mihomo YAML 中的 `proxies`。
- 为节点生成本地代理端口,同一端口同时支持 SOCKS5 与 HTTP/HTTPS CONNECT；默认仅本机可用，也可在设置中按需共享给局域网设备。
- HTTP 代理模式支持普通 HTTP 请求和 HTTPS CONNECT,兼容不支持 SOCKS5 的软件。
- 普通协议走 `sing-box`，`VLESS + XHTTP` 自动走 `xray` fallback。
- 菜单栏 App 默认不内置 `sing-box` / `xray`，首次运行时会自动下载到用户数据目录;也可构建时选择内置核心。
- 节点操作列可启动**独立指纹浏览器**(每节点独立指纹 + 独立 Profile),并自动绑定该节点的本地代理。

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
- 每个节点都有 `测试` / `重测`，会临时启动该节点并通过 SOCKS5 访问 `cp.cloudflare.com/generate_204`，显示真实可通信状态和延迟。
- `测试可见节点` 会按当前筛选结果批量测试。
- 控制台底部的 `连接日志` 会显示通过本地代理的 CONNECT/HTTP 目标、客户端本地端口、使用节点、代理协议、上下行流量、耗时和成功/失败状态。
- 运行中的每个本地代理卡片会显示累计上行、下行和合计流量,并分别展示 SOCKS5 URL 与 HTTP/HTTPS 代理 URL。
- 连接日志支持关键词搜索、按节点筛选和按状态筛选。
- 控制台侧边栏「通用设置」可开启**局域网共享**；开关会立即重绑正在运行的代理端口，并展示局域网设备可直接使用的 SOCKS5 / HTTP 地址。

连接日志记录的是本地代理层能看到的目标域名/IP 与端口；HTTPS 通过 CONNECT 建立隧道,内容不会被解密，第一版也不会自动识别 macOS 进程名。

菜单栏版会把数据写到：

```text
~/Library/Application Support/ProxyBridge
```

浏览器 Profile 会保存在同一目录下的 `browser_profiles/`(按节点隔离,持久保留 Cookies、LocalStorage、SessionStorage);Python 运行时自动安装在 `python-runtime/`;代理核心自动安装在 `proxy-cores/`;指纹内核安装在 `fingerprint-chromium/`。方案 B 使用打过补丁的 Chromium 指纹内核(基于 Ungoogled Chromium),从 C++ 底层伪造 Canvas、WebGL、AudioContext、字体、屏幕分辨率、`hardwareConcurrency`、`deviceMemory`、Client Hints、User-Agent 等全部指纹维度:

- 每个节点从 `node_id` 确定性派生固定的 `--fingerprint` 种子——**同一节点每次启动指纹完全一致(环境可恢复),不同节点指纹互不相同(避免被网站关联为同一设备)**。
- 时区(`--timezone`)与语言(`--lang` / `--accept-lang`)按节点地区自动匹配,使浏览器时区/语言与代理出口地区一致,降低不一致被风控识别的概率。
- 使用代理时强制 `--disable-non-proxied-udp`,防止 WebRTC 通过 STUN 泄漏真实 IP。

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

```bash
# 默认监听 127.0.0.1:8787，数据目录为仓库下 ./work
python3 app.py

# 指定端口 / 工作目录，并自动打开系统浏览器
python3 app.py --port 8790 --workdir ~/.proxybridge --open

# Windows PowerShell 示例
python app.py --port 8790 --workdir $env:APPDATA\ProxyBridge --open
```

打开：

```text
http://127.0.0.1:8787
```

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

## Windows / Linux 托盘常驻壳

在无壳 Web 控制台之上，额外提供系统托盘常驻入口（打开控制台 / 重启后端 / 停止全部代理 / 退出）。

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

### 在 Windows 上构建

```powershell
# 需要已安装 Python 3.10+
.\scripts\build_windows_exe.ps1
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

- 外层 SOCKS5 / HTTP 代理会监听 `0.0.0.0`，但只接受本机与 RFC1918 私有 IPv4 来源（`10/8`、`172.16/12`、`192.168/16`）；设置页会优先显示物理网卡的私有 IP 和可复制地址。
- 正在运行的代理会立即重新绑定，无需重启 App；切换时已有代理连接会中断并需重新连接。
- 管理控制台及管理 API 仍只监听 `127.0.0.1`，不会向局域网开放。
- 代理端口当前没有用户名或密码；可达本机的私有网络/VPN 客户端都可能使用。请只在可信网络开启，并留意 macOS 防火墙是否允许入站连接。

关闭开关后，运行中的代理会立即恢复为仅本机监听。

## WebDAV 同步

支持在多台 Mac 之间同步**订阅 / 节点**与**指纹浏览器 Profile**（Cookie、LocalStorage 等），用于换机接力。

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
- 轮换密钥后旧链接立即失效（控制台「轮换密钥」，或 `POST /api/pool/token/rotate`）。
- 代理池 HTTP 接口和管理控制台始终只监听 `127.0.0.1`；即使开启局域网共享，远端设备也只能使用设置页列出的直连代理地址。access log 会脱敏 path 中的 token。

## 数据位置

Web 控制台模式：

- 状态文件：`work/state.json`
- 上一份有效状态备份：`work/state.json.bak`
- 运行配置：`work/runtime/*.json`
- 核心日志：`work/runtime/*.log`

状态保存采用临时文件 + `fsync` + 原子替换；主状态损坏或缺失时会尝试读取 `.bak`，两者都不可用时会拒绝以空状态启动，避免静默丢失数据。数据目录权限为 `0700`，状态与运行日志等敏感文件为 `0600`。

第一版会把节点参数保存在本地明文状态文件中。后续版本建议接入 macOS Keychain。
