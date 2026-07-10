# ProxyBridge Project-Wide Stability Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复全项目审查中已经稳定复现的状态一致性、安全、代理传输、异步竞态和进程生命周期缺陷，并删除明确无效的前端状态代码。

**Architecture:** 保持现有单文件 Python 后端、无构建前端和原生 Swift 菜单栏结构不变。修复按状态/解析、安全边界、代理数据流、运行时生命周期、前端状态和菜单栏生命周期六个责任域展开；Python 行为全部先用 `unittest` 复现，前端用隔离工作目录的本地端到端场景复现，Swift 用后端回归测试、类型检查和端口冲突场景验证。

**Tech Stack:** Python 3.10+ 标准库、JavaScript/DOM、Node.js 语法检查、Swift/AppKit/WebKit、Bash、`unittest`。

## Global Constraints

- 覆盖 `app.py`、`static/`、`macos/ProxyBridgeMenuBar/`、`scripts/`、现有测试和相关文档。
- 每个行为修复必须由失败测试、稳定复现步骤、编译错误或明确静态证据支持。
- 采用最小修复；不进行大型文件拆分、框架迁移、界面重做或无关格式化。
- 仅删除确认未使用、重复、不可达或被等价实现完全覆盖的代码。
- 保留现有公开接口、状态文件结构、代理地址格式和用户可见工作流，除非它们本身构成已证实的缺陷。
- 不依赖真实订阅凭据，不把用户本地运行数据或生成物纳入提交。
- Python 行为修改必须遵循 RED → GREEN → 全量回归；Swift/JavaScript 在缺少现成单元测试框架处必须先完成隔离复现，再修改并重复同一场景。

---

### Task 1: 修复订阅状态一致性和协议字段归一化

**Files:**
- Create: `tests/test_state_and_parsing.py`
- Modify: `app.py:559-579`
- Modify: `app.py:804-839`
- Modify: `app.py:891-929`
- Modify: `app.py:1394-1449`

**Interfaces:**
- Consumes: `parse_link(raw, source) -> Node | None`、`sing_box_config(node, local_port) -> dict`、`load_state() -> dict`。
- Produces: `referenced_node_ids(subscriptions: list[dict[str, Any]]) -> set[str]`；合法且类型规范的状态字典；不会破坏共享节点引用的导入、刷新与删除行为。

- [x] **Step 1: 写入状态与 gRPC 的失败回归测试**

在新测试文件中用 `tempfile.TemporaryDirectory()` 临时替换 `WORK_ROOT`、`RUNTIME_ROOT` 和 `STATE_PATH`，并分别覆盖：

```python
def test_refresh_replaces_nodes_removed_from_same_url(self):
    app.import_subscription({"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"})
    app.import_subscription({"name": "same", "url": "https://example.test/sub", "raw": "vless://b@example.net:443#B"})
    self.assertEqual([node["name"] for node in app.load_state()["nodes"]], ["B"])

def test_delete_preserves_node_referenced_by_another_subscription(self):
    shared_link = "vless://shared@example.com:443#Shared"
    first = app.import_subscription({"name": "one", "url": "https://one.test/sub", "raw": shared_link})
    app.import_subscription({"name": "two", "url": "https://two.test/sub", "raw": shared_link})
    app.delete_subscription(first["subscription"]["id"])
    state = app.load_state()
    self.assertEqual(len(state["subscriptions"]), 1)
    self.assertEqual(len(state["nodes"]), 1)

def test_manual_imports_receive_distinct_ids_with_same_second(self):
    old_now = app.now_ts
    try:
        app.now_ts = lambda: 42
        first = app.import_subscription({"name": "manual", "raw": "vless://a@example.com:443#A"})
        second = app.import_subscription({"name": "manual", "raw": "vless://b@example.com:443#B"})
        self.assertNotEqual(first["subscription"]["id"], second["subscription"]["id"])
    finally:
        app.now_ts = old_now

def test_load_state_normalizes_valid_json_with_invalid_shapes(self):
    for payload in ([], None, {"bridges": None}, {"nodes": [None, "bad"], "tests": []}):
        app.STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")
        state = app.load_state()
        self.assertIsInstance(state, dict)
        self.assertIsInstance(state["bridges"], list)
        self.assertIsInstance(state["nodes"], list)
        self.assertIsInstance(state["tests"], dict)

def test_vless_grpc_service_name_reaches_sing_box_config(self):
    node = app.parse_link("vless://id@example.com:443?type=grpc&security=tls&serviceName=orders#grpc")
    config = app.sing_box_config(app.asdict(node), 21080)
    self.assertEqual(config["outbounds"][0]["transport"]["service_name"], "orders")
```

- [x] **Step 2: 运行新测试并确认按目标缺陷失败**

Run: `python3 -m unittest tests.test_state_and_parsing -v`

Expected: 刷新后仍出现 `A`、删除后节点数为 `0`、手动导入 ID 相同、异常形状抛出异常、gRPC `service_name` 为空。

- [x] **Step 3: 在同一状态事务内维护引用并规范输入**

实施以下最小逻辑：

```python
def referenced_node_ids(subscriptions: list[dict[str, Any]]) -> set[str]:
    return {
        str(node_id)
        for subscription in subscriptions
        if isinstance(subscription, dict)
        for node_id in subscription.get("node_ids") or []
        if node_id
    }
```

- `load_state()` 在调用 `setdefault()` 前验证根对象为字典，并把列表字段过滤为字典条目、把 `tests` 规范为字典。
- `parse_vless()` 把小写查询键 `servicename`、`service_name`、`service-name` 归一到 `settings["service_name"]`；Clash `grpc-opts` 同样归一。
- URL 相同的订阅导入时，先移除旧订阅，再仅删除已不被剩余订阅引用的旧节点，随后合并新节点。
- 删除订阅时只删除不再被其他订阅引用的节点。
- 订阅 ID 使用 `time.time_ns()` 并包含原始内容摘要，避免同秒碰撞，不改变已有 ID。

- [x] **Step 4: 运行局部与全量 Python 测试**

Run: `python3 -m unittest tests.test_state_and_parsing -v`

Expected: PASS。

Run: `python3 -m unittest discover -s tests -v`

Expected: 原有与新增测试全部通过；端口测试在受限沙箱内仍可明确显示 SKIP。

- [x] **Step 5: 提交状态修复**

```bash
git add app.py tests/test_state_and_parsing.py
git commit -m "fix: preserve subscription state invariants"
```

### Task 2: 阻止归档越界与跨站本地 API 调用

**Files:**
- Create: `tests/test_security.py`
- Modify: `app.py:37-65`
- Modify: `app.py:1130-1147`
- Modify: `app.py:3125-3252`

**Interfaces:**
- Consumes: `_safe_extract_tar(archive_path, dest)`、`AppHandler.headers`、`AppHandler.read_json()`。
- Produces: `_is_loopback_host(value: str) -> bool`、`AppHandler.is_trusted_request() -> bool`；只接受本地 Host/Origin 与 JSON 变更请求的 API。

- [x] **Step 1: 写入解压和请求来源失败测试**

测试必须用真实 tar 和真实 `email.message.Message`/`io.BytesIO`，覆盖：

```python
def test_tar_rejects_symlink_pivot(self):
    # tar: pivot -> ../outside，然后写 pivot/escaped.txt
    with self.assertRaises(RuntimeError):
        app._safe_extract_tar(archive_path, destination)
    self.assertFalse((outside / "escaped.txt").exists())

def test_handler_rejects_remote_host_origin_and_cross_site_fetch(self):
    handler = object.__new__(app.AppHandler)
    for headers in (
        {"Host": "evil.example"},
        {"Host": "127.0.0.1:8787", "Origin": "https://evil.example"},
        {"Host": "127.0.0.1:8787", "Sec-Fetch-Site": "cross-site"},
    ):
        handler.headers = Message()
        for name, value in headers.items():
            handler.headers[name] = value
        self.assertFalse(handler.is_trusted_request())

def test_read_json_rejects_non_json_and_oversized_body(self):
    # application/json 正常；text/plain 和超过 API_JSON_BODY_LIMIT 的长度分别拒绝。
```

- [x] **Step 2: 运行测试并确认当前 tar 越界和来源校验缺失**

Run: `python3 -m unittest tests.test_security -v`

Expected: tar 测试发现目标目录外文件；`AppHandler.is_trusted_request` 不存在；`read_json` 接受未限制长度。

- [x] **Step 3: 实施兼容 Python 3.10 的最小安全边界**

- `_safe_extract_tar()` 拒绝符号链接、硬链接、块/字符设备和 FIFO，只允许普通文件与目录，再执行路径包含检查和解压。
- 增加 `API_JSON_BODY_LIMIT = 1_048_576`。
- `is_trusted_request()` 要求 Host 的 hostname 属于 `127.0.0.1`、`localhost` 或 `::1`；Origin 存在时也必须是本地 HTTP(S) 来源；`Sec-Fetch-Site: cross-site` 直接拒绝。
- `do_GET()`、`do_HEAD()`、`do_POST()` 在访问 API 或静态内容前拒绝不可信请求；`do_POST()` 仅接受 `application/json`。
- `read_json()` 验证 `Content-Length` 为 `0..API_JSON_BODY_LIMIT`。

- [x] **Step 4: 运行安全测试、全部 Python 测试和语法检查**

Run: `python3 -m unittest tests.test_security -v`

Expected: PASS。

Run: `PYTHONPYCACHEPREFIX=/tmp/proxybridge-pycache python3 -m py_compile app.py tests/*.py`

Expected: exit 0。

- [x] **Step 5: 提交安全修复**

```bash
git add app.py tests/test_security.py
git commit -m "fix: harden archives and local api requests"
```

### Task 3: 保留 TCP 半关闭响应并终止 HTTP 代理连接复用

**Files:**
- Create: `tests/test_proxy_regressions.py`
- Modify: `app.py:2675-2747`
- Modify: `app.py:2835-2870`
- Modify: `tests/test_parser.py:1241-1272`

**Interfaces:**
- Consumes: `Socks5LogProxy._relay(client, upstream) -> dict[str, int]`、`_build_http_forward_head(...) -> bytes`。
- Produces: 正确传播单向 EOF 的双向 relay；明确向上游发送 `Connection: close` 的单请求 HTTP 转发。

- [x] **Step 1: 先写会暴露响应截断与 keep-alive 的测试**

```python
def test_relay_preserves_response_after_client_half_closes(self):
    # client_peer 发送 request 后 shutdown(SHUT_WR)；upstream_peer 读到 EOF 后发送 response 并半关闭。
    # 断言 client_peer 收到完整 response，upload/download 计数均正确，relay 线程退出。

def test_http_forward_forces_upstream_connection_close(self):
    request = proxy._parse_http_request(
        b"GET http://example.com/a HTTP/1.1\r\nHost: example.com\r\nConnection: keep-alive\r\nProxy-Connection: keep-alive\r\n\r\n"
    )
    _, _, head, _ = proxy._http_forward_plan(request)
    self.assertNotIn(b"Connection: keep-alive", head)
    self.assertIn(b"Connection: close\r\n", head)
```

把现有“任一侧关闭即立即全关闭”的测试改为上述正确半关闭语义，而不是继续固化错误行为。

- [x] **Step 2: 运行代理回归测试并观察失败**

Run: `python3 -m unittest tests.test_proxy_regressions -v`

Expected: response 为空或不完整；转发头仍含 `Connection: keep-alive`。

- [x] **Step 3: 最小修改 relay 和请求头**

- 正常 EOF 只对目标 socket 调用 `shutdown(SHUT_WR)`，不设置全局完成事件。
- OSError、显式 `_stop` 或不可恢复异常才全关闭两端；双方 pump 都结束后返回计数。
- HTTP 普通请求过滤 `Connection`、`Proxy-Connection`、`Keep-Alive` 和 `Proxy-Authorization`，统一添加 `Connection: close`，确保一次解析对应一次上游连接。
- 在拆分请求头后再次检查实际长度，堵住单次 `recv()` 越过限制的边界。

- [x] **Step 4: 运行局部测试和受允许的真实 socket 测试**

Run: `python3 -m unittest tests.test_proxy_regressions -v`

Expected: PASS。

Run outside restricted port sandbox when approved: `python3 -m unittest discover -s tests -v`

Expected: 包括 SOCKS5、HTTP CONNECT、HTTP absolute-form 在内的全部测试 PASS，无 SKIP。

- [x] **Step 5: 提交代理语义修复**

```bash
git add app.py tests/test_proxy_regressions.py tests/test_parser.py
git commit -m "fix: preserve proxy half-close semantics"
```

### Task 4: 序列化重复启动并缩短整体关闭时间

**Files:**
- Create: `tests/test_lifecycle_regressions.py`
- Modify: `app.py:55-65`
- Modify: `app.py:1751-1836`
- Modify: `app.py:1839-1968`
- Modify: `app.py:2165-2230`
- Modify: `app.py:3289-3319`

**Interfaces:**
- Consumes: `start_bridge(payload) -> dict`、`launch_browser(payload) -> dict`、`download_fingerprint_chromium(payload) -> dict`、`shutdown()`。
- Produces: 每节点最多一个活动 Bridge；每实例最多一个活动浏览器进程；指纹内核安装单飞；并发清理所有独立进程组。

- [x] **Step 1: 写入重复启动与并发清理失败测试**

用现有 FakeProcess/FakePopen 模式，但断言真实生命周期结果：

```python
def test_start_bridge_returns_existing_active_bridge(self):
    first = app.start_bridge({"node_id": node_id, "core": "auto"})
    second = app.start_bridge({"node_id": node_id, "core": "auto"})
    self.assertEqual(second["id"], first["id"])
    self.assertEqual(FakePopen.call_count, 1)

def test_running_browser_instance_cannot_be_launched_twice(self):
    first = app.launch_browser({"node_id": node_id})
    with self.assertRaisesRegex(ValueError, "已在运行"):
        app.launch_browser({"node_id": node_id, "instance_id": first["instance_id"]})
    self.assertEqual(len(FakePopen.calls), 1)

def test_fingerprint_install_is_single_flight(self):
    # 两线程同时进入真实 download_fingerprint_chromium，外层网络/DMG 操作用受控临时替身。
    # 断言安装临界区 max_active == 1，第二次在锁内重新检查并返回 already_installed。

def test_shutdown_stops_independent_processes_concurrently(self):
    # terminate_process 替身用 Barrier 证明多个清理任务同时进入，而不是逐个等待。
```

- [x] **Step 2: 运行生命周期测试并确认重复创建与串行清理**

Run: `python3 -m unittest tests.test_lifecycle_regressions -v`

Expected: 第二个 Bridge/浏览器被创建；安装临界区并发数大于 1；关闭任务无法同时到达 Barrier。

- [x] **Step 3: 加入最小锁和幂等检查**

- `start_bridge()` 在 `STATE_LOCK` 内同步进程状态后，如果该节点已有活动 Bridge，直接返回它。
- 用 `BROWSER_LAUNCH_LOCK` 包住一次完整浏览器启动；同步实例状态后，活动实例返回明确错误，避免覆盖 `BROWSER_PROCESS_TABLE[instance_id]`。
- 用 `FINGERPRINT_INSTALL_LOCK` 包住“检查已有安装→下载→安装→刷新缓存”全过程，等待者进锁后重新检查。
- `shutdown()` 同时启动浏览器终止和 Bridge `stop_process()` 工作线程，收集 traffic 后一次写回状态；保持重复调用幂等。

- [x] **Step 4: 运行生命周期与全量测试**

Run: `python3 -m unittest tests.test_lifecycle_regressions -v`

Expected: PASS。

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS（受限环境的端口用例除外）。

- [x] **Step 5: 提交运行时生命周期修复**

```bash
git add app.py tests/test_lifecycle_regressions.py
git commit -m "fix: serialize runtime lifecycle operations"
```

### Task 5: 修复 Web 控制台异步状态并删除无效状态

**Files:**
- Modify: `static/app.js:1-25`
- Modify: `static/app.js:213-310`
- Modify: `static/app.js:651-869`
- Modify: `static/styles.css:150-162`

**Interfaces:**
- Consumes: `/api/status`、`/api/clipboard`、`/api/browser/download-kernel`。
- Produces: 只提交最新状态请求；筛选 UI 与内部状态一致；复制只有实际成功才提示；内核下载在 DOM 重绘后仍保持单飞。

- [x] **Step 1: 在隔离后端上复现四个前端问题**

启动：`PROXY_BRIDGE_WORKDIR=/tmp/proxybridge-review PROXY_BRIDGE_PORT=18788 python3 app.py`。

使用浏览器控制台/自动化依次验证：

1. 选择随后会消失的协议，更新状态后下拉显示“全部”但节点列表为空。
2. 让两个 `loadStatus()` 以 B 后 A 的顺序完成，旧 A 覆盖新 B。
3. 让 `/api/clipboard` 返回 `{ok:false}` 且移除 Clipboard API，当前仍显示成功。
4. 让内核下载 Promise 保持 pending 并触发一次轮询，重绘的新按钮可以再次提交。

Expected: 四个场景均复现当前错误。

- [x] **Step 2: 实施最小前端状态修复**

```javascript
let latestStatusRequest = 0;
let kernelDownloadPromise = null;

async function loadStatus() {
  const requestId = ++latestStatusRequest;
  const status = await api("/api/status");
  if (requestId !== latestStatusRequest) return false;
  // 现有状态赋值与 render()
  return true;
}
```

- `setSelectOptions()` 返回实际选中值，`renderFilters()` 回写 `appState.filters.protocol` 和 `region`。
- `copyText()` 仅在后端 `ok === true` 或一次 `navigator.clipboard.writeText()` 成功后显示成功，否则抛出“当前环境无法复制到剪贴板”。
- `downloadKernel()` 复用 `kernelDownloadPromise`；`renderCores()` 根据该 Promise 给重建按钮加 `disabled` 与 `is-busy`。
- 删除只写不读的 `appState.chrome`、`appState.activeView`，以及行为等价为空操作的 `syncModalOpenState()`、调用点和 `body.modal-open` 重复 CSS；保留后端公开的 `status.chrome` 字段。

- [x] **Step 3: 重复隔离场景并运行静态检查**

Run: `node --check static/app.js`

Expected: exit 0。

重复 Step 1；Expected: 筛选回退到全部并显示节点、旧响应不覆盖新状态、复制失败显示错误、内核下载只提交一次。

- [x] **Step 4: 提交前端修复**

```bash
git add static/app.js static/styles.css
git commit -m "fix: stabilize web console state"
```

### Task 6: 修复菜单栏后端伪就绪和辅助进程退出

**Files:**
- Modify: `macos/ProxyBridgeMenuBar/main.swift:300-430`
- Modify: `macos/ProxyBridgeMenuBar/main.swift:683-795`
- Modify: `macos/ProxyBridgeMenuBar/main.swift:940-970`

**Interfaces:**
- Consumes: `BridgeAPI.getStatus`、Swift `Process`、Python `/api/status`。
- Produces: 仅在后端实际响应后启用菜单；后端意外退出时清除伪状态；退出时取消 bootstrap helper；重复 node ID 不再触发字典 fatal error。

- [x] **Step 1: 复现菜单栏崩溃和伪就绪**

- 用两个相同 `nodeId` 的 `Bridge` 值执行现有 `Dictionary(uniqueKeysWithValues:)`，确认触发 duplicate-key fatal error。
- 在 18787 端口先启动占位监听，再启动菜单栏 App；确认 Python bind 失败后 `api != nil` 仍使菜单项可用。
- 用限速 helper 替代下载命令，在安装中退出，确认现有退出路径不登记也不停止 helper。

- [x] **Step 2: 先让启动状态取决于真实健康检查**

- `startBackend()` 创建局部 `candidateAPI`，成功 `process.run()` 后保持 `self.api == nil`。
- 新增 `waitForBackendReady(candidateAPI:process:attemptsRemaining:)`，每 200ms 调用 `/api/status`；仅在响应成功且 `backendProcess === process`、`process.isRunning` 时赋值 `api` 和 `latestStatus`。
- 在重试耗尽或进程退出时清空 `api/backendProcess/latestStatus`、关闭对应日志并显示具体错误。
- 给 backend `Process` 设置常驻 termination handler，按对象身份忽略旧进程回调；App 正在退出时不弹出新错误。

- [x] **Step 3: 登记 bootstrap helper 并防御重复 Bridge**

- 用 `NSLock` 保护 `helperProcesses` 和 `isTerminating`；`runProcess()` 在 `process.run()` 前后登记/移除 helper。
- `applicationWillTerminate()` 先设置退出标志，终止 helper，等待 bootstrap 工作组完成清理，再终止后端；后端并发清理完成的上限与 Swift 等待窗口保持一致。
- 启动安装前删除仅匹配 `python-runtime-install-*` 和 `python-runtime-staged-*` 的旧临时目录。
- 把 `Dictionary(uniqueKeysWithValues:)` 改为 `reduce(into:)`；旧状态有重复节点时按后端数组顺序保留最后一条活动 Bridge，不再 fatal。

- [x] **Step 4: 类型检查并重复端口冲突/退出场景**

Run: `xcrun swiftc -frontend -parse macos/ProxyBridgeMenuBar/main.swift`

Expected: exit 0。

Run when the local SDK/toolchain pair supports it:

```bash
xcrun swiftc -typecheck -target arm64-apple-macos13.0 -module-cache-path /tmp/proxybridge-swift-cache -framework AppKit -framework Foundation -framework WebKit macos/ProxyBridgeMenuBar/main.swift
```

Expected: exit 0；若本机 SDK 与 compiler build 不匹配，记录完整版本错误并使用构建脚本/匹配工具链复核。

重复 Step 1；Expected: 重复 node 不崩溃、端口冲突不启用菜单、退出后无 helper 残留。

- [x] **Step 5: 提交菜单栏生命周期修复**

```bash
git add macos/ProxyBridgeMenuBar/main.swift
git commit -m "fix: harden menubar process lifecycle"
```

### Task 7: 全量验证、独立复审和风险记录

**Files:**
- Modify: `docs/superpowers/plans/2026-07-10-project-wide-stability-review.md`（只勾选已完成步骤，不改写验收标准）

**Interfaces:**
- Consumes: Tasks 1–6 的全部提交。
- Produces: 可重复的最终验证证据和剩余风险清单。

- [x] **Step 1: 运行所有离线质量门**

```bash
python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/proxybridge-pycache python3 -m py_compile app.py tests/*.py
node --check static/app.js
bash -n scripts/build_menubar_app.sh
plutil -lint macos/ProxyBridgeMenuBar/Info.plist
xcrun swiftc -frontend -parse macos/ProxyBridgeMenuBar/main.swift
xcrun swiftc -frontend -parse scripts/generate_app_icon.swift
git diff --check
```

Expected: 所有命令成功；受限沙箱导致的端口 SKIP 单独在下一步消除。

- [x] **Step 2: 在获批的非网络沙箱环境运行真实端口测试**

Run: `python3 -m unittest discover -s tests -v`

Expected: 全部测试 PASS，`test_socks5_log_proxy_records_connect`、`test_http_connect_proxy_records_connect`、`test_http_proxy_rewrites_absolute_form_request` 不再 SKIP。

- [x] **Step 3: 运行隔离 UI 冒烟测试**

使用 `/tmp/proxybridge-review` 和非默认端口启动后端，确认导入、筛选、启动按钮错误处理、日志模态框和浏览器记录模态框；不下载真实核心、不读写用户状态。

- [x] **Step 4: 请求独立代码复审**

复审者逐项核对设计说明、计划、diff、测试输出，重点检查：状态引用集合、tar 成员类型、半关闭线程退出、锁顺序、Swift 回调身份、前端旧响应抑制及是否存在无关重构。

- [x] **Step 5: 记录不在稳健修复范围内的剩余风险**

明确记录：动态 `latest` Python 运行时和代理核心仍依赖上游 HTTPS/release 账户信任；若要加入固定版本、SHA-256 或签名 manifest，需要独立发布策略设计与更新流程，不能作为本轮临时常量写入。

- [ ] **Step 6: 最终状态检查**

Run: `git status --short --branch`

Expected: 仅有计划内文件变更，工作树无临时测试数据、缓存、构建产物或用户状态文件。
