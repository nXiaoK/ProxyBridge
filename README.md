# Proxy Bridge

本地订阅节点到 SOCKS5 的 macOS 菜单栏工具。

## 功能

- 导入订阅 URL 或原始订阅内容。
- 解析 `VLESS`、`VMess`、`Shadowsocks/SS2022`、`Trojan`、`Hysteria2/HY2`、`TUIC`、`SOCKS5`、`NaiveProxy`、`AnyTLS` 分享链接。
- 尝试解析 Clash/Mihomo YAML 中的 `proxies`。
- 为节点生成本地 `socks5://127.0.0.1:端口`。
- 普通协议走 `sing-box`，`VLESS + XHTTP` 自动走 `xray` fallback。
- 菜单栏 App 可内置 `sing-box` / `xray`，复制到其他 Apple Silicon Mac 后无需再单独安装代理核心。
- 节点操作列可启动**独立指纹浏览器**(每节点独立指纹 + 独立 Profile),并自动绑定该节点的本地 SOCKS5 代理。

## 运行要求

- Apple Silicon Mac，macOS 13+。
- 目标 Mac 需要有 `python3`，菜单栏 App 内部后端由 Python 启动。
- 构建出的 App 已内置 `sing-box` / `xray` 代理核心。

## 构建机准备

构建 App 时会从构建机复制 `sing-box` / `xray` 到 App 内部，因此构建机需要先安装：

```bash
brew install sing-box
brew install xray
```

也可以通过环境变量指定二进制和 Xray 资源路径：

```bash
SING_BOX_BIN=/path/to/sing-box \
XRAY_BIN=/path/to/xray \
XRAY_ASSET_DIR=/path/to/share/xray \
scripts/build_menubar_app.sh
```

## 菜单栏 App

构建好的 App 位于：

```text
outputs/ProxyBridgeMenuBar.app
```

双击后会在菜单栏显示 `PB`：

- 左键点击 `PB`：打开内置控制台面板。
- 右键点击 `PB`：打开快捷菜单。
- 控制台里可以导入订阅、筛选节点、启动并复制 SOCKS5、停止端口。
- 节点操作列的 `浏览器` 按钮会用**指纹内核**(fingerprint-chromium)创建/复用该节点专属 Profile,底层伪造整套浏览器指纹,并绑定该节点 SOCKS5。若节点还未启动 SOCKS5,会先自动启动;若指纹内核缺失,会提示先下载。
- 每个节点都有 `测试` / `重测`，会临时启动该节点并通过 SOCKS5 访问 `cp.cloudflare.com/generate_204`，显示真实可通信状态和延迟。
- `测试可见节点` 会按当前筛选结果批量测试。
- 控制台底部的 `连接日志` 会显示通过本地 SOCKS5 的 CONNECT 目标、客户端本地端口、使用节点、上下行流量、耗时和成功/失败状态。
- 运行中的每个 SOCKS5 卡片会显示累计上行、下行和合计流量。
- 连接日志支持关键词搜索、按节点筛选和按状态筛选。

连接日志记录的是 SOCKS5 层能看到的目标域名/IP 与端口；HTTPS 内容不会被解密，第一版也不会自动识别 macOS 进程名。

菜单栏版会把数据写到：

```text
~/Library/Application Support/ProxyBridge
```

浏览器 Profile 会保存在同一目录下的 `browser_profiles/`(按节点隔离,持久保留 Cookies、LocalStorage、SessionStorage);指纹内核安装在 `fingerprint-chromium/`。方案 B 使用打过补丁的 Chromium 指纹内核(基于 Ungoogled Chromium),从 C++ 底层伪造 Canvas、WebGL、AudioContext、字体、屏幕分辨率、`hardwareConcurrency`、`deviceMemory`、Client Hints、User-Agent 等全部指纹维度:

- 每个节点从 `node_id` 确定性派生固定的 `--fingerprint` 种子——**同一节点每次启动指纹完全一致(环境可恢复),不同节点指纹互不相同(避免被网站关联为同一设备)**。
- 时区(`--timezone`)与语言(`--lang` / `--accept-lang`)按节点地区自动匹配,使浏览器时区/语言与 SOCKS5 出口地区一致,降低不一致被风控识别的概率。
- 使用代理时强制 `--disable-non-proxied-udp`,防止 WebRTC 通过 STUN 泄漏真实 IP。

指纹内核约 150–250MB,可点击控制台顶部的 `下载指纹内核` 一键安装(按本机架构从 GitHub Releases 下载并去除 Gatekeeper 隔离属性),或手动下载后用环境变量 `PROXY_BRIDGE_FP_CHROMIUM` 指向 Chromium 可执行文件。由于该内核并非每个版本都发布 macOS 包,如自动下载未命中可稍后重试或手动放置。

如果 App 是从本项目的 `outputs/` 目录启动，首次运行会尝试迁移旧 Web 版的 `work/state.json`。

## 重新构建 App

```bash
scripts/build_menubar_app.sh
```

构建脚本会自动生成图标，并把 `sing-box`、`xray`、`geoip.dat`、`geosite.dat` 复制到 App 的 `Contents/Resources/bridge/bin/`。

如果复制到其他 Mac 后提示“无法验证开发者”或“已损坏”，可以在目标 Mac 执行：

```bash
xattr -dr com.apple.quarantine /Applications/ProxyBridgeMenuBar.app
```

## Web 控制台兼容模式

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

## 数据位置

Web 控制台模式：

- 状态文件：`work/state.json`
- 运行配置：`work/runtime/*.json`
- 核心日志：`work/runtime/*.log`

第一版会把节点参数保存在本地明文状态文件中。后续版本建议接入 macOS Keychain。
