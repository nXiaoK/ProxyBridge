# Proxy Bridge

本地订阅节点到多协议代理端口的 macOS 菜单栏工具。

## 功能

- 导入订阅 URL 或原始订阅内容。
- 解析 `VLESS`、`VMess`、`Shadowsocks/SS2022`、`Trojan`、`Hysteria2/HY2`、`TUIC`、`SOCKS5`、`NaiveProxy`、`AnyTLS` 分享链接。
- 尝试解析 Clash/Mihomo YAML 中的 `proxies`。
- 为节点生成本地代理端口,同一端口同时支持 `socks5://127.0.0.1:端口` 与 `http://127.0.0.1:端口`。
- HTTP 代理模式支持普通 HTTP 请求和 HTTPS CONNECT,兼容不支持 SOCKS5 的软件。
- 普通协议走 `sing-box`，`VLESS + XHTTP` 自动走 `xray` fallback。
- 菜单栏 App 默认不内置 `sing-box` / `xray`，首次运行时会自动下载到用户数据目录;也可构建时选择内置核心。
- 节点操作列可启动**独立指纹浏览器**(每节点独立指纹 + 独立 Profile),并自动绑定该节点的本地代理。

## 运行要求

- Apple Silicon Mac，macOS 13+。
- 菜单栏 App 不再要求目标 Mac 预装 `python3`；首次运行会自动下载独立 Python 运行时。
- 默认轻量 App 首次运行需要能访问 GitHub,用于自动下载 Python 运行时和 `sing-box` / `xray` 代理核心。

## 构建机准备

默认构建轻量 App，不会把 `sing-box` / `xray` 打进 App 包里：

```bash
scripts/build_menubar_app.sh
```

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

## Web 控制台兼容模式

直接运行 Web 控制台仍需要本机命令行环境提供 `python3`：

```bash
python3 app.py
```

打开：

```text
http://127.0.0.1:8787
```

如果要改控制台端口：

```bash
PROXY_BRIDGE_PORT=8790 python3 app.py
```

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
- 可选：关闭浏览器后自动上传（v1.1）、启动时检查远端更新（v1.1）。
- 可选：OpenSSL AES-256-CBC 端到端加密（v2）；本机需有 `openssl`。
- 冲突策略为远端后写覆盖（LWW），并记录设备 ID。

远端目录结构：

```text
{remote_root}/
  manifest.json
  state.sync.json
  profiles/{instance_id}.tar.gz
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
- 服务仍只监听 `127.0.0.1`；access log 会脱敏 path 中的 token。

## 数据位置

Web 控制台模式：

- 状态文件：`work/state.json`
- 运行配置：`work/runtime/*.json`
- 核心日志：`work/runtime/*.log`

第一版会把节点参数保存在本地明文状态文件中。后续版本建议接入 macOS Keychain。
