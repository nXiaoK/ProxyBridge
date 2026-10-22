# 使用与构建参考

本文保留各运行方式、配置、数据与开发验证的详细说明。首次使用请先阅读 [项目首页](../README.md)；以下命令默认在仓库根目录执行。

- [下载发布版](#下载自动打包版本)与[软件更新](#软件更新)
- [macOS 构建](#构建机准备)与[菜单栏 App](#菜单栏-app)
- [本地导入内核](#本地导入代理与指纹内核)
- [源码 Web 控制台](#无壳跨平台-web-控制台linux--windows--macos)
- [Docker](#docker-运行)与[Windows / Linux 托盘](#windows--linux-托盘常驻壳)
- [局域网共享](#局域网共享)、[WebDAV](#webdav-同步)、[代理池与网关](#代理池)
- [诊断](#诊断中心)、[数据位置](#数据位置)与[测试](#测试)

## 下载自动打包版本

可直接从 [GitHub Releases](https://github.com/nXiaoK/ProxyBridge/releases/latest) 下载：

- `ProxyBridge-macOS-arm64-<版本>.zip`：Apple Silicon、macOS 13+ 菜单栏 App。
- `ProxyBridge-Windows-x64-<版本>.exe`：Windows 10/11 x64 便携单文件。
- `ProxyBridge-macOS-arm64-full-<版本>.zip`：完整版 macOS App，额外内置 sing-box、Xray 与指纹浏览器内核的官方安装包。
- `ProxyBridge-Windows-x64-full-<版本>.zip`：完整版 Windows 便携包，解压后包含 `ProxyBridge.exe` 与 `bundled\` 目录（内置同样三个官方安装包），两者需放在同一目录。
- `SHA256SUMS.txt`：上述产物的 SHA-256 校验值。
- `ProxyBridge-payload-<版本>.zip`、`update-manifest.json`、`update-manifest.json.sig`：供应用内[软件更新](#软件更新)使用，无需手动下载。

完整版首次启动时，若工作目录中还没有对应组件，会按「本地导入」相同的校验流程从包内安装（指纹内核只在尚未安装任何版本时安装），无需再从 GitHub 下载代理核心和指纹内核；之后仍可在内核管理中升级、切换或删除，内核列表中来源显示为「离线包」。macOS 完整版首次启动仍会下载 Python 运行时（本机已有 Python 3.10+ 时直接使用）。内置版本取自打包时上游的最新发布：sing-box / Xray 取最新版本，指纹内核取最近一个提供该平台安装包的版本。

仓库根目录 `VERSION` 是人工维护的基础版本，当前为 `0.3.0`。`main` 每次 push 都会触发发布门禁与双平台打包；成功后使用 `0.3.0-build.<Actions 运行序号>` 创建唯一 Release。修改基础版本时只改 `VERSION`，并同步 macOS 源 plist 中的可读默认值。

自动构建的 macOS App 仅使用 ad-hoc 签名、未做 Apple 公证；Windows EXE 也未购买代码签名证书。首次启动可能遇到 Gatekeeper 或 SmartScreen 提示，处理方式见下方平台说明。

## 软件更新

macOS 菜单栏 App 与 Windows 便携 exe 支持应用内更新：在 **设置 → 通用 → 软件更新** 中检查并安装 GitHub 最新 Release。默认在后端启动 90 秒后及每 12 小时自动检查一次，只提示、不自动安装；关闭「自动检查更新」后只在手动点击时检查。

程序分为两层，更新时只替换变化的一层：

| 层 | 内容 | 更新方式 |
|---|---|---|
| 后端载荷 | `app.py` 等后端模块、`static/` 前端、`vendor/` 依赖 | 下载约 1–2 MB 的 `ProxyBridge-payload-<版本>.zip`，解压到数据目录，重启后端即生效；App / exe 本身不变 |
| 外壳 | macOS Swift 菜单栏程序、Windows 托盘 exe | 只有外壳源文件变化时才需要：macOS 原子替换 `.app` 后自动重新打开；Windows 由新 exe 等待旧进程退出后覆盖旧 exe 并重新启动 |

每个 Release 附带 `update-manifest.json`，记录版本、载荷与安装包的 SHA-256，以及 macOS / Windows 外壳修订号（外壳源文件的 SHA-256 汇总）。已安装外壳的修订号与清单一致时只热更新载荷，否则走整包替换。清单由发布流水线用 Ed25519 私钥签名（`update-manifest.json.sig`），客户端用内置公钥校验；签名缺失或不匹配时只提示手动下载，不会安装。

安装前会重新获取 Release 并再次验证签名与 SHA-256。安装更新会重启后端，运行中的代理和指纹浏览器会被停止，随后按原设置恢复。

回退与故障处理：

- 新载荷首次启动必须在 HTTP 服务监听后确认；导入或启动失败时自动回退到外壳内置后端，并在软件更新卡片中提示该版本曾启动失败。
- 运行已安装载荷时可点击「回退到内置版本」，停用该载荷并重启后端。
- 排障时可设置环境变量 `PROXY_BRIDGE_PAYLOAD_DISABLED=1` 启动，强制忽略已安装的载荷。

以下情况只提示新版本、不自动安装：源码运行（使用 `git pull`）、Docker（重新构建镜像）、Linux 托盘、清单未签名，以及 macOS App 位于系统隔离位置（App Translocation，需先移到「应用程序」文件夹）或所在目录不可写。整包替换时 macOS 完整版会换成轻量版外壳；已安装的代理核心与指纹内核保存在数据目录，不受影响。

数据目录中的更新文件位于 `app-updates/`：

| 路径 | 说明 |
|---|---|
| `app-updates/active.json` | 当前启用的载荷：版本、所需外壳修订号、`pending`（待确认）或 `confirmed` |
| `app-updates/payloads/<版本>/` | 解压后的载荷；启动时自动清理未启用的旧版本 |
| `app-updates/failed.json` | 启动失败或被手动回退的版本记录 |
| `app-updates/check.json` | 最近一次检查结果，仅用于界面显示 |

外壳与后端之间通过以下环境变量约定运行形态，均由外壳或启动器自动设置，通常无需手动配置：

| 环境变量 | 说明 |
|---|---|
| `PROXY_BRIDGE_SHELL_KIND` | 支持应用内更新的外壳类型：`macos-app` 或 `windows-exe`；未设置表示只提示新版本 |
| `PROXY_BRIDGE_SHELL_PATH` | 外壳自身位置（`.app` 目录或 `ProxyBridge.exe`），整包替换时使用 |
| `PROXY_BRIDGE_SHELL_ROOT` | 外壳内置后端目录；运行载荷时仍从这里查找 `bin/`、`bundled/` |
| `PROXY_BRIDGE_SHELL_REVISION` | 当前外壳修订号，来自构建时写入的 `SHELL_REVISION` 文件 |
| `PROXY_BRIDGE_PAYLOAD_VERSION` | 正在运行的载荷版本；为空表示运行内置后端 |
| `PROXY_BRIDGE_PAYLOAD_DISABLED` | 设为 `1` 时忽略已安装的载荷 |

### 发布签名密钥

发布流水线从仓库 Secret `PROXY_BRIDGE_UPDATE_SIGNING_KEY` 读取签名私钥（64 位十六进制的 Ed25519 种子），对应公钥写在 `app_update.py` 的 `TRUSTED_SIGNING_KEYS` 中。未配置 Secret 时 Release 仍会生成，但清单未签名，客户端只提示手动下载；私钥与内置公钥不匹配时发布任务直接失败。

```bash
# 生成新的私钥（只在需要新密钥时执行），并打印对应公钥用于 TRUSTED_SIGNING_KEYS
python3 -c "import secrets; print(secrets.token_hex(32))" > update-signing-key.hex
python3 -c "import sys; sys.path.insert(0, '.'); import app_update; print(app_update.ed25519_public_key(bytes.fromhex(open('update-signing-key.hex').read().strip())).hex())"
# 把私钥写入仓库 Secret；私钥文件妥善备份，不要提交到仓库
gh secret set PROXY_BRIDGE_UPDATE_SIGNING_KEY -R nXiaoK/ProxyBridge < update-signing-key.hex
```

更换密钥时，先把新公钥追加到 `TRUSTED_SIGNING_KEYS` 并发布一个版本，再替换 Secret；否则已安装的客户端无法验证新清单，只能手动升级一次。

本地可用 `scripts/build_update_release.py` 复现发布产物：`shell-revision --shell macos-app|windows-exe` 输出外壳修订号，`payload --version <版本> --out <目录>` 生成载荷，`manifest ...` 生成清单并在设置了签名私钥时签名。macOS / Windows 构建脚本会把外壳修订号写入包内 `SHELL_REVISION`；发布流水线通过 `PROXY_BRIDGE_SHELL_REVISION` 注入同一个值。

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

本地复现发布版本时可设置 `PROXY_BRIDGE_VERSION=0.3.0-build.12` 与 `PROXY_BRIDGE_BUILD_NUMBER=12`。前者写入应用界面和包内 `VERSION`，后者写入 macOS `CFBundleVersion` 或 Windows 四段文件版本；构建号必须是非负纯数字。

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

构建与 Release 相同的完整版时，先下载官方安装包，再用 `--bundle-packages` 放入 App（安装包保持官方文件名，首次启动自动安装，用户仍可在内核管理中升级）：

```bash
# 按目标平台选择与在线下载相同的官方包，并按 GitHub 提供的 SHA-256 校验；可选 GITHUB_TOKEN 仅用于提高 API 频率上限
python3 scripts/fetch_bundled_packages.py --os darwin --arch arm64 --out bundled-packages
scripts/build_menubar_app.sh --bundle-packages bundled-packages
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
- 节点操作中的 `浏览器` 按钮使用 fingerprint-chromium 创建独立浏览器记录与 Profile，并绑定该节点代理；已有记录可从指纹浏览器页复用。若节点代理尚未启动，会先自动启动；指纹内核缺失时会提示先安装。
- 每个节点都有 `测试` / `重测`，会临时启动该节点并通过 SOCKS5 访问设置中的探测目标，显示通信状态、延迟、成功率、结果年龄与过期状态。
- `测试可见节点` 会按当前筛选结果使用 3–5 路有界并发批量测试，支持逐项结果、失败重试和取消；单次上限 500 个节点。
- 「日志 → 连接」 会显示通过本地代理的 CONNECT/HTTP 目标、客户端本地端口、使用节点、代理协议、上下行流量、耗时和成功/失败状态。
- 运行中的每个本地代理卡片会显示累计上行、下行和合计流量,并分别展示 SOCKS5 URL 与 HTTP/HTTPS 代理 URL。
- 连接日志支持关键词搜索、按节点/状态筛选、分页，以及 CSV/JSON 导出。
- 控制台「设置 → 通用」可开启**局域网共享**；开关会立即重绑正在运行的代理端口，并展示局域网设备可直接使用的 SOCKS5 / HTTP 地址。

连接日志记录的是本地代理层能看到的目标域名/IP 与端口；HTTPS 通过 CONNECT 建立隧道,内容不会被解密，第一版也不会自动识别 macOS 进程名。

菜单栏版会把数据写到：

```text
~/Library/Application Support/ProxyBridge
```

浏览器 Profile 保存在同一目录下的 `browser_profiles/`，按节点与浏览器记录隔离，持久保留 Cookies、LocalStorage、SessionStorage。Python 运行时位于 `python-runtime/`，代理核心位于 `proxy-cores/`，指纹内核位于 `fingerprint-chromium/`，扩展库位于 `browser_extensions/library/`。fingerprint-chromium 可调整 Canvas、WebGL、AudioContext、字体、屏幕、User-Agent 等浏览器指纹信息。

### 指纹浏览器配置

每条浏览器记录都可以在「指纹浏览器 → 配置」中单独设置，也可多选后批量配置。修改在下次启动时生效：

| 配置 | 取值与说明 |
|---|---|
| 指纹种子 | 每条记录固定一个 `--fingerprint` 种子，Canvas、WebGL、Audio、字体等特征由它派生。「重新生成指纹」会换一个随机种子，已登录网站会把它视为新设备。 |
| 操作系统 | `跟随系统`（本机为宿主系统，Docker worker 为 Linux）或 Windows / macOS / Linux。与真实系统不同的平台仍使用真实系统的字体，可能被识别为不一致。 |
| 浏览器品牌 | Chrome（新记录默认）、Edge、Opera、Vivaldi 或 Chromium（内核默认）；可选填品牌版本，留空跟随内核版本。 |
| CPU 核心数 / 平台版本 / 窗口尺寸 | 留空或「随种子」时由内核决定；窗口尺寸格式为 `宽x高`。 |
| 关闭伪装项 | Canvas、Audio、字体、ClientRects、WebGL/GPU，需要内核 144 及以上。实测 macOS 上 Canvas 噪声与字体伪装会被 Pixelscan 判定为「Masking detected」，因此 macOS 新建记录默认关闭这两项；关闭后同一台 Mac 上各环境的 Canvas 与字体相同（与大量同型号 Mac 一致），GPU 型号、音频、ClientRects 仍按种子区分。配置弹窗中的「一键应用检测友好设置」与批量配置可把已有记录改成该组合。 |
| 时区 / 语言来源 | `按出口 IP`（新记录默认）：启动前经该节点代理访问 `ipwho.is` 查询出口时区与国家，结果按节点缓存 10 分钟，查询失败时回退到节点地区；`按节点地区`：使用节点名识别的地区表；`自定义`：手动填写 IANA 时区、语言与 Accept-Language。语言另有 `跟随系统`：不传语言参数，界面语言、`navigator.language`、`Intl` 区域与 Accept-Language 都使用系统设置。所有平台新建记录的时区与语言都默认按出口 IP。macOS 版 Chromium 的 `Intl` 区域与日期格式只跟随系统语言，语言与系统不同时会出现可被检测的不一致；需要规避时可改为「跟随系统」，或使用「一键应用检测友好设置」（macOS 上会同时把语言改为跟随系统）。 |
| 地理位置权限 | `禁止`（新记录默认）写入 Profile 默认权限，避免系统 / Wi-Fi 定位绕过代理暴露真实位置；`询问` 保持 Chromium 默认行为。 |
| 内核版本 | 跟随默认版本，或固定某个已安装版本；固定版本缺失时启动会报错，不会悄悄换内核。 |
| 扩展 | 从扩展库勾选。启动时扩展复制到 Profile 内的 `ProxyBridgeExtensions/`，随 Profile 一起同步，Docker worker 同样可用。 |
| 启动网址 | 每次启动打开的 http(s) 地址，最多 10 个；留空则首次打开出口检测页、之后打开空白页。 |
| CDP 调试端口 | 默认关闭，仅本机模式可用。开启后以 `--remote-debugging-port=0` 监听 127.0.0.1 随机端口，用于 Cookie 导入导出，也可以把记录中显示的 `ws://` 地址交给 Playwright / Puppeteer 的 `connectOverCDP`。该端口没有密码，本机任何程序都能控制浏览器，不用时请关闭。 |

- 升级前创建的记录保持原来的启动参数（Chromium 品牌、按节点地区定时区、地理位置询问），不会因为升级而改变已登录账号看到的指纹；需要时可在配置中手动改为推荐值。
- 传给内核的 `--accept-lang` 只包含语言列表（如 `en-US,en`），请求头中的 `q` 权重由 Chromium 自动生成；参数中带 `;q=` 会让 `navigator.languages` 出现异常条目。
- 「Cookie 与检测」中可一键打开 BrowserScan、Pixelscan、CreepJS 检测页；开启 CDP 并运行后，可导入 / 导出 JSON（Cookie-Editor、EditThisCookie、CDP 格式）或 Netscape `cookies.txt`。导出内容含登录凭据，请妥善保管。
- 「换绑节点」把已停止的记录移到另一个节点，Profile 目录随之移动，指纹种子与 Cookie 不变；早期的「默认浏览器」记录不支持换绑。
- 订阅刷新导致节点 ID 变化（例如机场修改了参数）时，绑定的浏览器记录与 Profile 会保留，原节点在节点列表中显示为「已从订阅移除 · 仅为浏览器记录保留」，换绑或删除最后一条记录后自动清理。手动删除订阅时默认同样保留，只有在确认框中勾选后才会连同 Profile 永久删除。
- macOS 上点击窗口左上角红色按钮关闭浏览器的最后一个窗口后，Chromium 默认仍驻留在 Dock；ProxyBridge 会为本机浏览器加载「ProxyBridge Window Helper」扩展，窗口全部关闭时立即通知控制台，按「停止」同样的流程结束浏览器（保留本地代理，并按设置自动上传 Profile），列表状态随之变为已停止。该扩展位于工作目录的 `browser_extensions/window_helper/`，不进入 Profile，没有内容脚本，网页无法探测；每次启动通过扩展目录内的 `config.json` 下发新的一次性令牌。只开一个窗口中的部分标签或关闭多窗口中的一个不会触发。设置环境变量 `PROXY_BRIDGE_BROWSER_STOP_ON_LAST_WINDOW=0` 可关闭此行为；控制台只监听局域网地址（`--host` 为具体 IP）时也不会启用。Windows 与 Linux 上关闭最后一个窗口本身就会退出浏览器，不加载该扩展。
- 实际指纹还会受内核版本和运行环境影响，不保证匿名性或无法被网站关联。
- 使用代理时强制 `--webrtc-ip-handling-policy=disable_non_proxied_udp`，阻止非代理 UDP 候选。

### 指纹内核版本

指纹内核约 150–250MB,可在「设置 → 内核管理」点击 `下载安装` / `下载最新版本` 一键安装(按本机架构从 GitHub Releases 下载并去除 Gatekeeper 隔离属性)，或用 `选择版本下载` 安装指定版本，也可点击 `本地导入` 上传完整发布包，或手动下载后用环境变量 `PROXY_BRIDGE_FP_CHROMIUM` 指向 Chromium 可执行文件。由于该内核并非每个版本都发布 macOS 包,如自动下载未命中可稍后重试或使用本地导入。

- 多个版本并存：每个版本安装到 `fingerprint-chromium/versions/<版本号>/`，目录下的 `.proxybridge-kernel.json` 记录版本、来源与可执行文件相对路径；升级前直接装在 `fingerprint-chromium/` 下的内核显示为「早期安装」并继续可用。
- 默认版本：未设定时使用最新已安装版本；在内核管理中「设为默认」后写入 `fingerprint-chromium/.proxybridge-default-kernel.json`（内容为 `{"id": "<版本号>"}`，空 ID 表示自动选择最新版本）。该文件只对本机生效，不参与 WebDAV 同步。设置了 `PROXY_BRIDGE_FP_CHROMIUM` 时，环境变量指定的内核始终是默认版本。
- 安装新版本不需要停止浏览器；原地替换已安装的同一版本或删除版本前需要停止所有浏览器。有记录固定使用的版本不能删除。

### 扩展库

在浏览器配置的「内核与扩展」中点击「上传扩展包」，可导入 `.zip`（解压后的扩展目录打包）或 `.crx` 文件，单个上限 64 MiB。扩展以安装包 SHA-256 前 16 位为 ID 保存在 `browser_extensions/library/<ID>/`，同一安装包重复上传不会产生重复条目。从扩展库删除扩展会把它从所有记录中移除，各 Profile 内的副本在下次启动时清理。

## 本地导入代理与指纹内核

当 NAS、服务器或当前电脑访问 GitHub Releases 很慢时，可先在网络正常的电脑上下载官方文件，再打开 ProxyBridge 控制台「设置 → 内核管理」的「本地导入」：

1. 选择 `sing-box`、`Xray` 或 `fingerprint-chromium`。
2. 选择未经改名的官方 Release 文件；页面会显示当前目标 OS/架构和支持格式。
3. 可选填写官方提供的 64 位 SHA-256；留空时服务端仍会计算实际 SHA-256 并在成功结果中返回。
4. 停止正在使用目标代理核心的本地代理；替换指纹内核前需停止所有浏览器实例，然后上传并安装。

常见 Linux amd64 文件名示例：

| 目标 | 文件示例 | 支持格式 |
|---|---|---|
| sing-box | `sing-box-1.13.15-linux-amd64-glibc.tar.gz` | `.tar.gz`、`.tgz`、`.zip` |
| Xray | `Xray-linux-64.zip` | `.zip` |
| fingerprint-chromium | `fingerprint-chromium_linux_x64.zip`（以实际上游文件名为准） | `.zip`、`.tar.gz`、`.tgz`、`.tar.xz`、`.tar`；macOS 另支持 `.dmg` |

必须保留官方文件名，因为后端会用其中的系统与 CPU 架构标记拒绝错误平台包。Docker 远程浏览器固定使用 Linux x86_64 指纹包，上传的必须是包含可执行 `chrome-wrapper` 及相邻资源的完整目录包，不能只上传单个 `chrome`，也不支持 `.7z`。单个上传文件最大 2 GiB，解压后总大小最大 4 GiB；文件按 1 MiB 分块写入私有暂存目录，不会整体读入服务端内存。

本地导入只验证文件结构、平台、可执行性及可选校验值，不能证明发布来源。只应从 sing-box、Xray 和 fingerprint-chromium 的可信官方 Release 获取文件。

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

> 完整安装/排障教程见 [Windows / Linux 安装教程](windows-linux-install.md)。

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
| `PROXY_BRIDGE_FP_CHROMIUM` | 手动指定指纹 Chromium 可执行文件；设置后始终作为默认内核，浏览器配置中固定的其他版本仍按各自版本启动 |
| `PROXY_BRIDGE_BUNDLED_DIR` | 离线包目录，内含 `sing-box/`、`xray/`、`fingerprint-chromium/` 子目录及官方安装包；未设置时查找程序资源目录下的 `bundled/`（macOS App 为 `Contents/Resources/bridge/bundled`）以及 Windows 可执行文件旁的 `bundled\`。对应组件未安装时首次启动自动从这里安装 |
| `PROXY_BRIDGE_BROWSER_STOP_ON_LAST_WINDOW` | 仅 macOS：默认开启，关闭浏览器最后一个窗口后自动停止该浏览器；设为 `0` / `false` / `off` 时保持 Chromium 的 Dock 驻留行为 |
| `PROXY_BRIDGE_PAYLOAD_DISABLED` | 设为 `1` 时忽略软件更新安装的后端载荷，使用 App / exe 内置后端；其他更新相关变量见[软件更新](#软件更新) |

说明：

- 无壳模式与菜单栏 App 共用同一套 `app.py` 后端与 `static/` 前端。
- 代理核心会按当前操作系统/架构从 GitHub Releases 自动选择 Linux / Windows / macOS 包；下载受阻时可通过控制台「本地导入」安装同一官方包。
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

默认 Compose 同时提供 Web 控制台、本地代理和远程指纹浏览器。浏览器不在主容器内运行：内部 Browser Broker 会为每条运行记录创建一个 `lscr.io/linuxserver/chromium` worker，并把该记录的固定指纹、独立 Profile 和节点 SOCKS5 代理交给它。未配置 Broker 的单容器 `docker run` 仍保持禁用浏览器的安全行为。

```bash
docker compose up -d --build
docker compose logs --tail=50 proxybridge
```

从最新启动日志中复制完整的 `Proxy Bridge console:` 地址，不要只打开裸端口地址：

```text
Proxy Bridge console: http://127.0.0.1:8787/#token=...
```

Compose 默认：

- 使用 `proxybridge-data` 命名卷持久化 `/data`。远程浏览器通过 Docker Engine API 1.45+ 的 `volume-subpath` 只挂载对应 Profile 与指纹内核，因此此模式不支持把 `/data` 改为宿主机 bind mount。
- Web 控制台默认仅发布到宿主机 `127.0.0.1:8787`；可通过 `.env` 中的 `PROXY_BRIDGE_CONSOLE_HOST=<宿主机局域网 IPv4>` 精确开放给局域网。
- 代理端口范围发布为 `127.0.0.1:21080-21179`，容器内自动分配也限制在相同范围。
- 首次启动自动下载 Linux 版 `sing-box` / `xray` 到数据卷；下载受阻时可从控制台上传官方包替代。
- 每个浏览器实例从 `23000-23099` 动态占用一个 Broker TLS TCP 中继端口；默认只绑定宿主机 `127.0.0.1`。worker 仍只加入 internal 网络，不直接发布 HostPort，也不能借入口网络绕过节点代理出网。
- worker 的 `/config` 使用随容器删除的匿名运行卷，只有 `/config/browser-profile` 指向持久化 Profile；Selkies 的 TLS、认证和其他运行文件不会进入 WebDAV Profile。
- 每个 worker 只能免认证连接分配给自己的节点代理端口；主服务按 Broker 返回的精确 worker IP 与代理端口配对校验，不信任整个 worker 子网。
- 浏览器功能启用时，WebDAV 可同步已停止的 Docker 浏览器 Profile；禁用 Broker 后才退回「仅订阅 / 节点」。
- Broker 启动时会回收上次异常退出遗留的 worker；Compose 为主服务和 Broker 都保留 3 分钟停止宽限期，以完成 worker 回收与 Profile 落盘。

首次使用时，先在控制台点击「下载指纹内核」，或通过「本地导入」上传 Linux x86_64 完整发布包，等待内核保存到 `/data/fingerprint-chromium/`。然后在节点行点击「浏览器」：弹窗默认用户名为 `proxybridge`，并在本地生成可复制、可修改的随机强密码；提交后会创建独立 worker，并自动打开返回的远程链接。第一次创建前还需拉取较大的 `lscr.io/linuxserver/chromium:latest` Worker 镜像；控制台会提示镜像是否已经缓存，网络受阻时可按 Docker 文档使用 `docker pull` 或在其他电脑 `docker save` 后到服务器 `docker load`。运行中的「浏览器记录」可再次「远程打开」「查看凭证」或「停止」；停止后凭证 secret 立即销毁。密码不会写入状态文件、状态 API 或 WebDAV。单条记录的「删除」会先停止 worker，再永久删除本地 Profile 和状态记录。

若要让同一可信局域网或 VPN 中的其他电脑直接打开控制台和浏览器链接，在仓库根目录创建 `.env`，三项均填写 Docker 宿主机的同一个私有 IPv4：

```dotenv
# Web 控制台发布地址；只填宿主机明确的私有 IPv4，不能填 0.0.0.0。
PROXY_BRIDGE_CONSOLE_HOST=192.168.1.20
# Browser Broker HTTPS 中继的宿主机绑定地址；只允许回环或宿主机明确的私有 IPv4。
PROXY_BRIDGE_BROWSER_HOST=192.168.1.20
# 控制台生成浏览器链接时使用的地址；通常必须与上一项相同。
PROXY_BRIDGE_BROWSER_PUBLIC_HOST=192.168.1.20
```

重新创建服务后，只在宿主机防火墙中向可信网段开放 TCP `8787` 和 `23000-23099`。每个 worker 会在本次容器的匿名 `/config` 运行卷中动态生成自签名证书，浏览器首次访问会显示证书警告；内置 Basic Auth 只适合作为可信 LAN/VPN 内的便利防护。**不得把这些端口直接发布到公网**；公网访问必须在前面部署使用受信任证书的 TLS 反向代理，并增加 SSO 或等效的强认证。

> Browser Broker 挂载了 `/var/run/docker.sock`。即使 Compose 标记为 `:ro`，Docker API 仍可创建/删除容器及挂载宿主机路径，权限事实上接近宿主机 root。不要在不可信或多租户 Docker 主机上部署，也不要把 Broker 控制网络或凭证卷暴露给其他容器。

完整的 `docker run`、端口扩容、数据卷、离线核心、SSH 隧道和排障步骤见 [Docker 运行教程](docker.md)。

## Windows / Linux 托盘常驻壳

在无壳 Web 控制台之上，额外提供系统托盘常驻入口（打开控制台 / 重启后端 / 停止全部代理 / 退出）。

> 分系统步骤、开机自启、依赖安装见 [Windows / Linux 安装教程](windows-linux-install.md)。

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

> 从构建到卸载的完整 Windows 教程见 [Windows / Linux 安装教程](windows-linux-install.md#21-方式-a双击-proxybridgeexe推荐)。

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

默认情况下，所有代理端口都只监听 `127.0.0.1`，其他局域网设备无法连接。在控制台进入 **「设置 → 通用」**，开启 **「允许局域网设备连接本地代理」** 后：

- 外层 SOCKS5 / HTTP 代理会监听 `0.0.0.0`；默认白名单仅包含本机与 RFC1918 私有 IPv4，也可在设置中配置最多 32 条 IP/CIDR。
- 正在运行的代理会立即重新绑定，无需重启 App；切换时已有代理连接会中断并需重新连接。
- 管理控制台及管理 API 仍只监听 `127.0.0.1`，不会向局域网开放。
- 新开启共享默认使用 Token，已有无认证共享保持原样；也可显式选择用户名密码或确认后关闭认证。SOCKS5 使用 RFC 1929 用户名密码认证，HTTP 代理接受 Basic/Bearer 凭据。
- 可设置全局活动连接上限；设置页显示当前客户端、来源 IP、代理和连接时长，并支持一键断开。

关闭开关后，运行中的代理会立即恢复为仅本机监听。

## WebDAV 同步

桌面/普通无壳模式支持同步**订阅 / 节点**与**指纹浏览器 Profile**（Cookie、LocalStorage 等），用于换机接力。

默认 Docker Compose 的远程浏览器也支持同步**订阅 / 节点**与**已停止的浏览器 Profile**。只有未配置 Broker 或显式禁用浏览器的 Docker/其他模式才会切换为**仅订阅 / 节点同步**：不会打包、下载或删除 Profile；如果相同远端目录中已有浏览器数据，上传会验证并保留它们。

控制台侧边栏 **「WebDAV 同步」**：

1. 填写 WebDAV URL（如坚果云 / Nextcloud / 群晖）、用户名密码与远端目录（默认 `ProxyBridge`）。
2. 勾选「启用同步」并保存。
3. **上传到远端**：打包已停止浏览器的 Profile + 订阅状态。
4. **从远端拉取**：覆盖本机订阅/节点/浏览器记录，并解压 Profile。
   若远端 `state_sha256` 与本机上次成功同步一致，则**跳过拉取**（已是最新）；需要强制覆盖时可在 API 传 `{"force": true}`。

约定：

- 运行中的浏览器**不会**被上传；请先关闭再同步。
- 不要两边同时打开同一浏览器实例。
- **删除订阅**默认保留关联节点的浏览器记录与 Profile（原节点转为保留节点，可换绑）；只有在确认框中勾选「同时永久删除」时才会停止并删除这些记录与本地 Profile。WebDAV 已启用时会**自动全量上传**，远端 `state` 与多余 `profiles/*.tar.gz` 一并清理。
- 浏览器记录的指纹配置、分组、固定内核版本、扩展列表与启动网址参与同步；CDP 端口、实际运行内核与生效时区等运行期信息不同步，Profile 中的 `DevToolsActivePort` 也不会打包。
- 在 macOS、Windows 或 Docker 的 **浏览器记录** 中单条点击「删除」，会先停止对应进程/worker，再永久删除该记录及本地 Profile；WebDAV 已启用时同样自动全量上传并清理远端记录与 Profile 包。
- 打开节点 **浏览器记录** 时（WebDAV 已启用）会先**自动拉取**一次远端；若检测到本地未上传变更，会保留本地记录并提示先上传，避免旧远端快照覆盖新记录。
- 浏览器记录按最近创建、启动或停止时间倒序显示，并支持 **标签**（如「访问 xx 的浏览器」）；保存标签会标记本地变更并尝试同步。
- 本机浏览器窗口退出、在控制台点浏览器「停止」，或在「运行中代理」停止关联节点（且开启「关闭后自动上传」）会**自动上传**该 Profile（含标签）；远程 worker 关闭网页标签不等于停止实例，需在「浏览器记录」中点击「停止」。
- 拉取时以远端为权威：本机多出来的订阅/节点/浏览器记录会被移除，对应本地 Profile 目录也会清理。
- 未配置 Broker 或显式禁用浏览器时，上述覆盖与清理只适用于订阅和节点，本地浏览器记录/Profile 保持不变。
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

运行中的本地代理会自动进入**代理池**（`status` 为 `running` / `starting` 且端口有效）。控制台「代理池与网关」页默认遮蔽密钥，点击复制获得完整链接；请妥善保管这些链接，不要将密钥或状态文件公开。

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

在「代理池与网关」页启用智能网关后，会创建一个稳定入口（默认 `21000`），客户端只需配置一次 SOCKS5 或 HTTP 地址。每个新连接根据所选策略从健康且已就绪的代理池成员中选择；连接建立后保持该节点，节点失败只影响当前连接。网关会把连接失败反馈给代理池熔断器，不重复计入普通 Bridge 流量。

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
# 禁止生成 .pyc 缓存，保持验证后的工作目录整洁
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node --check static/auth.js
node --check static/diagnostics.js
node --check static/browser-ui.js
node --check static/core-upload.js
node --check static/app.js
node --check static/main.mjs
for file in static/pages/*.js; do node --check "$file"; done
node --test tests/test_frontend_*.js
bash -n scripts/*.sh
git diff --check
```
