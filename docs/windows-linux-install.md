# Windows / Linux 安装与使用教程

本文面向希望在 **Windows** 或 **Linux** 上使用 ProxyBridge 的用户。  
macOS 用户优先使用菜单栏 App；需要时也可参考本文的无壳 / 托盘方式。

ProxyBridge 在 Windows / Linux 上提供两种形态：

| 形态 | 适合谁 | 是否需要 Python | 入口 |
|---|---|---|---|
| **托盘常驻壳** | 日常桌面使用 | 源码运行需要；`.exe`/单文件二进制不需要 | 托盘图标 + Web 控制台 |
| **无壳 Web 控制台** | 服务器、调试、脚本化 | 需要 | `python app.py` 后用浏览器打开 |
| **Docker** | NAS、服务器、容器化长期运行 | 不需要宿主机 Python | `docker compose up -d --build` |

核心能力相同：

- 导入订阅 / 节点
- 启动本地 SOCKS5 + HTTP 代理
- 测速
- 连接日志
- Web 控制台管理

---

## 1. 先看你该用哪种方式

### Windows 推荐

1. **有现成 `ProxyBridge.exe`**：双击运行（最简单）
2. **没有 exe，但有源码**：在 Windows 上构建 exe，或直接用 Python 跑托盘/无壳模式

### Linux 推荐

1. **桌面环境（GNOME / KDE / 等）**：托盘壳
2. **无桌面 / SSH 服务器**：无壳 Web 控制台
3. **需要单文件分发**：构建 Linux 二进制
4. **已经使用 Docker / NAS**：参考 [Docker 运行教程](docker.md)

---

## 2. Windows 安装与使用

### 2.1 方式 A：双击 `ProxyBridge.exe`（推荐）

#### 前提

- Windows 10/11 x64（常见）
- 能访问 GitHub 自动下载 `sing-box` / `xray`，或可在别的电脑下载官方包后从控制台本地导入
- 不强制预装 Python

#### 步骤

1. 获取 `ProxyBridge.exe`  
   - 自己构建：见 [2.3](#23-方式-c在-windows-上构建-proxybridgeexe)  
   - 或从 [GitHub Releases](https://github.com/nXiaoK/ProxyBridge/releases/latest) 下载名称为 `ProxyBridge-Windows-x64-<版本>.exe` 的自动发布文件
2. 把 `ProxyBridge.exe` 放到任意目录，例如：
   ```text
   D:\Apps\ProxyBridge\ProxyBridge.exe
   ```
3. 双击运行
4. 若 Windows SmartScreen 提示“未知发布者”：
   - 点 **更多信息**
   - 再点 **仍要运行**
5. 看到托盘图标后，浏览器会自动打开带当前实例令牌的控制台，地址形如：
   ```text
   http://127.0.0.1:18787/#token=...
   ```
6. 在控制台中：
   - 导入订阅
   - 启动节点代理
   - 复制 SOCKS5 / HTTP 地址给浏览器或其他软件

#### 托盘菜单

- **打开控制台**
- **重启后端**
- **停止全部代理**
- **开机自启**
- **退出**

#### 创建桌面图标 / 开机启动（可选）

若你本地有源码仓库，可在 PowerShell 中执行：

```powershell
.\scripts\create_windows_shortcuts.ps1 -ExePath .\outputs\windows\release\ProxyBridge.exe
```

这会创建：

- 桌面快捷方式
- 开机启动快捷方式（带 `--no-open`，登录后不自动弹浏览器）

也可以在托盘菜单直接勾选 **开机自启**。

#### 数据与日志位置

| 类型 | 路径 |
|---|---|
| 数据目录 | `%APPDATA%\ProxyBridge` |
| 状态文件 | `%APPDATA%\ProxyBridge\state.json` |
| 托盘日志 | `%APPDATA%\ProxyBridge\tray-host.log` |
| 后端日志 | `%APPDATA%\ProxyBridge\tray-backend.log` |
| 代理核心 | `%APPDATA%\ProxyBridge\proxy-cores\` |
| 浏览器 Profile | `%APPDATA%\ProxyBridge\browser_profiles\` |

PowerShell 打开数据目录：

```powershell
explorer $env:APPDATA\ProxyBridge
```

---

### 2.2 方式 B：源码 + Python 运行（开发/调试）

#### 前提

- Python 3.10+（建议 3.11/3.12）
- Git（可选）

#### 安装

```powershell
cd C:\path\to\ProxyBridge
python -m pip install -r desktop\requirements.txt
```

#### 启动托盘

```powershell
.\scripts\run_tray.ps1
# 或
python -m desktop.tray_host --port 18787
```

#### 仅启动 Web 控制台（无托盘）

```powershell
python app.py --port 8787 --open
```

`--open` 会自动打开认证地址，终端也会打印类似下面的启动行：

```text
Proxy Bridge console: http://127.0.0.1:8787/#token=...
```

#### 开机自启（源码模式）

```powershell
python -m desktop.tray_host --enable-autostart
python -m desktop.tray_host --autostart-status
python -m desktop.tray_host --disable-autostart
```

---

### 2.3 方式 C：在 Windows 上构建 `ProxyBridge.exe`

在 **Windows** 机器执行（需要已安装 Python）：

```powershell
cd C:\path\to\ProxyBridge
.\scripts\build_windows_exe.ps1
```

如果 `python` 命中了 Microsoft Store 的 `WindowsApps\python.exe` 执行别名，请改用真实 Python 路径：

```powershell
.\scripts\build_windows_exe.ps1 -Python "C:\Users\<用户名>\AppData\Local\Programs\Python\Python312\python.exe"
```

成功后产物：

```text
outputs\windows\release\ProxyBridge.exe
```

可选创建快捷方式：

```powershell
.\scripts\create_windows_shortcuts.ps1 -ExePath .\outputs\windows\release\ProxyBridge.exe
```

说明：

- 构建使用 PyInstaller onefile
- 生成的是**便携绿色版**，不写注册表安装
- 当前默认未代码签名，SmartScreen 可能提示

---

### 2.4 Windows 日常使用流程

1. 启动 ProxyBridge（exe 或托盘）
2. 打开控制台
3. **导入订阅**
4. 对节点点 **启动**
5. 复制：
   - `socks5://127.0.0.1:端口`
   - 或 HTTP 代理 `http://127.0.0.1:端口`
6. 填到浏览器 / 终端 / 其他支持代理的软件
7. 用 **测试** 看延迟与连通性
8. 不用时在控制台停止节点，或托盘里 **停止全部代理**
9. 完全退出时点托盘 **退出**

#### 局域网设备共用（手机/其他电脑）

1. 控制台进入 **设置 → 通用**
2. 开启 **允许局域网设备连接本地代理**
3. 使用页面展示的局域网 IP + 端口  
4. 仅在可信网络开启；新开启共享默认使用 Token，已有无认证共享保持原样，请核对认证与 IP/CIDR 白名单

---

### 2.5 Windows 常见问题

**Q: 双击没反应？**  
A: 查看 `%APPDATA%\ProxyBridge\tray-host.log` 与 `tray-backend.log`。

**Q: 控制台打不开？**  
A: 确认托盘还在，然后用托盘菜单的 **打开控制台** 重新进入，不要手动输入裸地址。若端口被占用，程序会尝试换端口，以日志为准。

**Q: 提示已有实例在运行？**  
A: 默认单实例。先退出旧托盘，或（源码模式）加 `--allow-multiple`。

**Q: 首次很慢？**  
A: 通常是在从 GitHub 下载 `sing-box` / `xray`。如果当前网络几乎无法完成下载，可在别的电脑取得保留官方文件名的 Windows x64 Release 包（如 `sing-box-版本-windows-amd64.zip`、`Xray-windows-64.zip`），再点击控制台「设置 → 内核管理」中的「本地导入」上传；替换前先停止使用对应核心的本地代理。

**Q: 指纹浏览器下载失败？**  
A: 上游不一定每次都发 Windows 包。若已有匹配 Windows/x86_64 的完整 `.zip` 发布包，可用控制台「本地导入」安装；也可手动下载后设置：

```powershell
$env:PROXY_BRIDGE_FP_CHROMIUM="C:\path\to\chrome.exe"
```

**Q: 如何彻底卸载便携版？**  
A:

1. 托盘退出
2. 删除 `ProxyBridge.exe`
3. 删除 `%APPDATA%\ProxyBridge`
4. 如启用过开机自启，删除：
   ```text
   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ProxyBridge.lnk
   ```

---

## 3. Linux 安装与使用

### 3.1 方式 A：托盘壳（桌面用户推荐）

#### 前提

- 图形桌面会话（`DISPLAY` 或 `WAYLAND_DISPLAY`）
- Python 3.10+
- 能访问 GitHub 自动下载核心，或可从控制台本地导入预先下载的官方包

#### Debian / Ubuntu 依赖示例

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv \
  python3-gi gir1.2-ayatanaappindicator3-0.1
```

> 不同发行版托盘后端可能不同。若托盘图标不显示，请安装对应桌面的 AppIndicator / StatusNotifier 支持。

#### 安装与启动

```bash
cd /path/to/ProxyBridge
python3 -m pip install -r desktop/requirements.txt

# 启动托盘
scripts/run_tray.sh
# 或
python3 -m desktop.tray_host --port 18787
```

默认会打开带当前实例令牌的地址，形如：

```text
http://127.0.0.1:18787/#token=...
```

#### 开机自启

托盘菜单勾选 **开机自启**，或：

```bash
python3 -m desktop.tray_host --enable-autostart
python3 -m desktop.tray_host --autostart-status
python3 -m desktop.tray_host --disable-autostart
```

会写入：

```text
~/.config/autostart/proxybridge.desktop
```

#### 数据与日志

| 类型 | 路径 |
|---|---|
| 数据目录 | `~/.local/share/ProxyBridge`（或 `$XDG_DATA_HOME/ProxyBridge`） |
| 托盘日志 | `.../tray-host.log` |
| 后端日志 | `.../tray-backend.log` |

---

### 3.2 方式 B：无壳 Web 控制台（服务器/无桌面）

适合 SSH 服务器、无 GUI 环境。

```bash
cd /path/to/ProxyBridge

# 可选：虚拟环境
python3 -m venv .venv
source .venv/bin/activate

python3 app.py --port 8787 --workdir ~/.local/share/ProxyBridge
```

终端会打印完整认证地址：

```text
Proxy Bridge console: http://127.0.0.1:8787/#token=...
```

复制 `Proxy Bridge console:` 后面的完整地址。页面接收令牌后会立即清除地址栏 fragment，并将令牌仅保存在当前标签页的会话状态中；刷新当前标签页仍可使用，新标签页需重新使用启动输出中的地址。

若要从另一台机器访问控制台，不建议直接把管理口暴露到公网。  
更稳妥：

- SSH 本地转发：
  ```bash
  ssh -L 8787:127.0.0.1:8787 user@server
  ```
- 然后在本机打开服务端启动输出中的完整认证地址；其 `127.0.0.1:8787` 部分会通过隧道转发

管理 API 默认按 loopback Host 校验，并要求当前后端实例令牌；局域网共享开关只影响**代理端口**，不会把控制台自动开放到局域网。

#### systemd 用户服务示例（可选）

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/proxybridge.service <<'UNIT'
[Unit]
Description=ProxyBridge headless console
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/github/ProxyBridge
ExecStart=%h/github/ProxyBridge/.venv/bin/python app.py --port 8787 --workdir %h/.local/share/ProxyBridge --no-core-auto-install
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now proxybridge.service
systemctl --user status proxybridge.service
```

> 按你的实际路径修改 `WorkingDirectory` / `ExecStart`。  
> 若希望首次自动下载核心，去掉 `--no-core-auto-install`。

---

### 3.3 方式 C：构建 Linux 单文件二进制

在 Linux 上：

```bash
cd /path/to/ProxyBridge
scripts/build_linux_bin.sh
```

产物：

```text
outputs/linux/dist/ProxyBridge
```

运行：

```bash
chmod +x outputs/linux/dist/ProxyBridge
./outputs/linux/dist/ProxyBridge --port 18787
```

说明：

- 需要桌面会话才能显示托盘
- 不同发行版 glibc 版本可能影响可移植性；最稳仍是同机构建同机运行

---

### 3.4 Linux 日常使用流程

1. 启动托盘或 `python3 app.py`
2. 打开 Web 控制台
3. 导入订阅
4. 启动节点
5. 复制本地代理地址给应用
6. 测速 / 看连接日志
7. 退出托盘或 `Ctrl+C` 结束无壳进程

本地代理示例：

```bash
# curl 走 SOCKS5
curl -x socks5h://127.0.0.1:21080 https://cp.cloudflare.com/generate_204 -I
```

---

### 3.5 Linux 常见问题

**Q: 提示未检测到图形会话？**  
A: 托盘壳需要桌面环境。服务器请用无壳模式 `python3 app.py`。

**Q: 托盘图标不显示？**  
A: 安装 AppIndicator / StatusNotifier 相关包；GNOME 可能需要扩展。不影响直接访问 Web 控制台。

**Q: 权限/端口失败？**  
A: 默认只绑 `127.0.0.1`。不要用 root 除非必要。

**Q: 如何换数据目录？**

```bash
python3 -m desktop.tray_host --workdir /data/proxybridge
# 或
python3 app.py --workdir /data/proxybridge
```

**Q: 如何卸载源码托盘？**

```bash
python3 -m desktop.tray_host --disable-autostart
rm -rf ~/.local/share/ProxyBridge
# 如使用虚拟环境，删除仓库目录即可
```

---

## 4. Web 控制台通用操作

无论 Windows 还是 Linux，控制台操作一致：

1. **导入订阅**
   - 填订阅 URL，或粘贴原始分享链接 / Clash YAML
2. **启动节点**
   - 生成本地端口（SOCKS5 + HTTP 同端口）
3. **复制代理地址**
   - 给系统代理、浏览器插件、终端工具使用
4. **测试节点**
   - 单节点或 3–5 路有界并发批量测试；可取消、失败重试并查看结果年龄
5. **连接日志**
   - 看访问目标、流量、成功/失败，按条件分页并导出 CSV/JSON
6. **局域网共享**
   - 配置 Basic/Token、IP/CIDR、连接上限并管理当前客户端
7. **WebDAV 同步**（可选）
   - 多设备同步订阅与浏览器 Profile
8. **诊断中心**
   - 先运行本地核心/端口/权限检查；确认目标后再勾选网络检查

---

## 5. 常用命令速查

### Windows

```powershell
# 构建 exe
.\scripts\build_windows_exe.ps1

# 创建桌面 + 开机快捷方式
.\scripts\create_windows_shortcuts.ps1 -ExePath .\outputs\windows\release\ProxyBridge.exe

# 源码托盘
python -m desktop.tray_host --port 18787

# 无壳控制台
python app.py --port 8787 --open

# 开机自启
python -m desktop.tray_host --enable-autostart
```

### Linux

```bash
# 托盘
python3 -m pip install -r desktop/requirements.txt
scripts/run_tray.sh

# 无壳
python3 app.py --port 8787 --workdir ~/.local/share/ProxyBridge

# 构建单文件
scripts/build_linux_bin.sh

# 开机自启
python3 -m desktop.tray_host --enable-autostart
```

### 环境变量

| 变量 | 含义 |
|---|---|
| `PROXY_BRIDGE_PORT` | 控制台端口 |
| `PROXY_BRIDGE_HOST` | 控制台绑定地址，默认 `127.0.0.1` |
| `PROXY_BRIDGE_WORKDIR` | 数据目录 |
| `PROXY_BRIDGE_INSTANCE_TOKEN` | 可选的管理 API 实例令牌；自动化场景可设置高强度随机值，普通使用留空即可 |
| `PROXY_BRIDGE_SING_BOX` | 手动指定 sing-box |
| `PROXY_BRIDGE_XRAY` | 手动指定 xray |
| `PROXY_BRIDGE_FP_CHROMIUM` | 手动指定指纹 Chromium |

---

## 6. 安全建议

- 管理控制台默认仅本机访问，保持这样更安全
- 管理 API 除 `HEAD /api/status` 健康检查外都要求 `X-ProxyBridge-Instance` 请求头；脚本调用时使用与 `PROXY_BRIDGE_INSTANCE_TOKEN` 相同的随机值
- 实例令牌主要防止本机跨站或误调用，不是多用户远程登录；不要公开带 `#token=...` 的启动地址或日志
- 代理端口仅在可信局域网共享，并配置 Token / Basic 认证及 IP/CIDR 白名单；已有无认证配置不会在升级时自动改变
- 状态文件含节点参数，注意数据目录权限
- 不要把 `ProxyBridge` 数据目录同步到公开网盘明文共享
- 公网机器优先用 SSH 隧道访问控制台，不要直接暴露

---

## 7. 相关文件

| 文件 | 说明 |
|---|---|
| `desktop/tray_host.py` | 托盘壳主程序 |
| `desktop/autostart.py` | 开机自启 |
| `scripts/run_tray.sh` / `scripts/run_tray.ps1` | 源码托盘启动 |
| `scripts/build_windows_exe.ps1` | Windows exe 构建 |
| `scripts/create_windows_shortcuts.ps1` | Windows 快捷方式 |
| `scripts/build_linux_bin.sh` | Linux 单文件构建 |
| `scripts/run_headless.sh` / `scripts/run_headless.ps1` | 无壳启动 |
| `app.py` | 后端 + Web 控制台 |

---

## 8. 最小验收清单

### Windows exe

- [ ] 双击 `ProxyBridge.exe` 后托盘出现
- [ ] 浏览器能打开控制台
- [ ] 能导入订阅并启动一个节点
- [ ] 本机可用 SOCKS5/HTTP 访问外网测试地址
- [ ] 诊断中心本地检查能返回逐项结果
- [ ] 托盘“开机自启”可开关
- [ ] “退出”后后端进程结束

### Linux 托盘 / 无壳

- [ ] 托盘或 `app.py` 可启动
- [ ] 控制台可打开
- [ ] 节点可启动/停止/测速
- [ ] 日志目录有 `tray-*.log` 或系统日志可见
- [ ] 诊断中心能识别核心、端口与数据目录状态
- [ ] 退出后端口释放
