# Lifecycle Resource Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Bridge, browser, and proxy-connection cleanup retry-safe by retaining ownership until termination succeeds and persisting only stable final traffic.

**Architecture:** Keep the existing runtime tables and public API, but apply one ownership rule to every stop path: snapshot a registered resource, terminate it, then conditionally remove that exact object. Add a small `bridge_id -> node_id` ownership index for process-only startup failures, and make `Socks5LogProxy.stop()` drain registered client handlers before traffic is snapshotted.

**Tech Stack:** Python 3 standard library (`threading`, `subprocess`, `unittest`, `unittest.mock`).

## Global Constraints

- Do not change HTTP API request or response shapes.
- Do not change the persisted state-file schema.
- A registered process, proxy, or connection may be deregistered only after confirmed termination.
- A termination failure must retain the original object reference for a later public-stop or shutdown retry.
- Conditional deletion must verify both the key and object identity.
- Bridge ownership begins when the core process is registered, before proxy creation or state persistence.
- `Socks5LogProxy.stop()` must not return successfully while a client handler can still add traffic.
- Do not add infinite retries or replace all runtime tables with a new manager.

---

### Task 1: Retain browser process ownership until termination succeeds

**Files:**
- Modify: `app.py:2115-2155`
- Test: `tests/test_lifecycle_regressions.py`

**Interfaces:**
- Consumes: `BROWSER_PROCESS_TABLE`, `BROWSER_TABLE`, `PROCESS_LOCK`, `terminate_process()`.
- Produces: `remove_registered_browser_process(process, instance_id=None) -> None`; retry-safe `stop_browser_instance_process()` and `stop_untracked_browser_processes_for_node()`.

- [ ] **Step 1: Add failing instance and node-level stop tests**

Add tests equivalent to the following inside `LifecycleRegressionTests`:

```python
def test_stop_browser_instance_keeps_handle_until_termination_succeeds(self) -> None:
    node_id = "node-browser-stop-retry"
    instance_id = "browser-stop-retry"
    process = FakeProcess(4801)
    app.BROWSER_PROCESS_TABLE[instance_id] = process
    app.BROWSER_TABLE[node_id] = [process]

    with mock.patch.object(app, "terminate_process", side_effect=RuntimeError("terminate failed")):
        with self.assertRaisesRegex(RuntimeError, "terminate failed"):
            app.stop_browser_instance_process(instance_id)

    self.assertIs(app.BROWSER_PROCESS_TABLE.get(instance_id), process)
    self.assertEqual(app.BROWSER_TABLE.get(node_id), [process])

    retried: list[int] = []
    with mock.patch.object(app, "terminate_process", side_effect=lambda item: retried.append(item.pid)):
        self.assertEqual(app.stop_browser_instance_process(instance_id), 1)
    self.assertEqual(retried, [4801])
    self.assertNotIn(instance_id, app.BROWSER_PROCESS_TABLE)
    self.assertNotIn(node_id, app.BROWSER_TABLE)

def test_stop_untracked_browsers_removes_only_successful_processes(self) -> None:
    node_id = "node-browser-batch-retry"
    failed = FakeProcess(4811)
    successful = FakeProcess(4812)
    app.BROWSER_TABLE[node_id] = [failed, successful]
    attempted: list[int] = []

    def terminate(item: FakeProcess) -> None:
        attempted.append(item.pid)
        if item is failed:
            raise RuntimeError("batch terminate failed")

    with mock.patch.object(app, "terminate_process", side_effect=terminate):
        with self.assertRaisesRegex(RuntimeError, "batch terminate failed"):
            app.stop_untracked_browser_processes_for_node(node_id)

    self.assertEqual(attempted, [4811, 4812])
    self.assertEqual(app.BROWSER_TABLE[node_id], [failed])
```

- [ ] **Step 2: Run the two tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_lifecycle_regressions.LifecycleRegressionTests.test_stop_browser_instance_keeps_handle_until_termination_succeeds \
  tests.test_lifecycle_regressions.LifecycleRegressionTests.test_stop_untracked_browsers_removes_only_successful_processes -v
```

Expected: the instance test shows the handle was removed after the first failure; the batch test shows the node list was popped before termination.

- [ ] **Step 3: Implement conditional removal after successful termination**

Use a single helper and preserve failures:

```python
def remove_registered_browser_process(
    process: subprocess.Popen,
    instance_id: str | None = None,
) -> None:
    with PROCESS_LOCK:
        if instance_id and BROWSER_PROCESS_TABLE.get(instance_id) is process:
            BROWSER_PROCESS_TABLE.pop(instance_id, None)
        remove_browser_process_from_node_tables(process)


def stop_browser_instance_process(instance_id: str) -> int:
    with PROCESS_LOCK:
        process = BROWSER_PROCESS_TABLE.get(instance_id)
    if process is None:
        return 0
    killed = 0
    if process.poll() is None:
        terminate_process(process)
        killed = 1
    remove_registered_browser_process(process, instance_id)
    return killed
```

For node-level untracked processes, snapshot without popping, attempt every process, and remove only successful or already-exited objects:

```python
def stop_untracked_browser_processes_for_node(node_id: str) -> int:
    with PROCESS_LOCK:
        procs = list(BROWSER_TABLE.get(node_id, []))
    killed = 0
    failures: list[BaseException] = []
    for process in procs:
        try:
            if process.poll() is None:
                terminate_process(process)
                killed += 1
        except BaseException as error:
            failures.append(error)
            continue
        remove_registered_browser_process(process)
    if failures:
        details = "; ".join(str(error) for error in failures)
        raise RuntimeError(f"停止部分浏览器失败（{len(failures)} 项）: {details}") from failures[0]
    return killed
```

Failed objects remain in `BROWSER_TABLE`; the loop still attempts later objects.

- [ ] **Step 4: Verify GREEN and lifecycle regression coverage**

Run the two-test command from Step 2, then:

```bash
python3 -m unittest tests.test_lifecycle_regressions -v
```

Expected: both new tests pass and the lifecycle module passes.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_lifecycle_regressions.py
git commit -m "fix: retain browser process ownership"
```

### Task 2: Track Bridge ownership from core-process creation

**Files:**
- Modify: `app.py:40-60`
- Modify: `app.py:1790-1885`
- Modify: `app.py:2425-2445`
- Test: `tests/test_lifecycle_regressions.py`

**Interfaces:**
- Consumes: `PROCESS_TABLE`, `LOG_PROXY_TABLE`, `PROCESS_LOCK`, `stop_process()`.
- Produces: `BRIDGE_NODE_TABLE: dict[str, str]`; process-only residual detection; conditional owner release after complete cleanup.

- [ ] **Step 1: Add a failing process-only residual test**

Add a test that starts a Bridge whose core never becomes ready and whose first termination fails:

```python
def test_process_only_bridge_residual_blocks_duplicate_start(self) -> None:
    node_id = "node-process-only-residual"
    self._write_state(node_id)
    ports = iter([24510, 25510, 24511, 25511])
    terminate_attempts: list[int] = []

    class FakePopen:
        calls: list["FakePopen"] = []

        def __init__(self, _command: list[str], **_kwargs: object) -> None:
            self.pid = 4820 + len(self.calls)
            self.calls.append(self)

        def poll(self) -> None:
            return None

    def fail_first_termination(process: FakePopen) -> None:
        terminate_attempts.append(process.pid)
        if len(terminate_attempts) == 1:
            raise RuntimeError("core cleanup failed")

    with (
        mock.patch.object(app, "generate_config", return_value={"test": True}),
        mock.patch.object(app, "core_command", return_value=["fake-core"]),
        mock.patch.object(app, "next_free_port", side_effect=lambda _preferred=None: next(ports)),
        mock.patch.object(app, "wait_for_port", return_value=False),
        mock.patch.object(app.subprocess, "Popen", FakePopen),
        mock.patch.object(app, "terminate_process", side_effect=fail_first_termination),
    ):
        with self.assertRaisesRegex(RuntimeError, "内部端口启动超时"):
            app.start_bridge({"node_id": node_id, "core": "auto"})
        with self.assertRaisesRegex(RuntimeError, "未清理|残留"):
            app.start_bridge({"node_id": node_id, "core": "auto"})

    self.assertEqual(len(FakePopen.calls), 1)
    self.assertEqual(len(app.PROCESS_TABLE), 1)

    with mock.patch.object(app, "terminate_process", return_value=None):
        app.shutdown()
    self.assertEqual(app.PROCESS_TABLE, {})
    self.assertEqual(app.BRIDGE_NODE_TABLE, {})
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_lifecycle_regressions.LifecycleRegressionTests.test_process_only_bridge_residual_blocks_duplicate_start -v
```

Expected: the second start creates another `FakePopen` or fails with the core timeout instead of the residual-resource error.

- [ ] **Step 3: Register and conditionally release Bridge ownership**

Add the table next to the other runtime tables:

```python
BRIDGE_NODE_TABLE: dict[str, str] = {}
```

When registering the core process, use the same lock:

```python
with PROCESS_LOCK:
    PROCESS_TABLE[bridge_id] = process
    BRIDGE_NODE_TABLE[bridge_id] = node_id
```

Before allocating ports, reject any owner for the node:

```python
with PROCESS_LOCK:
    residual_bridge_ids = [
        bridge_id
        for bridge_id, owner_node_id in BRIDGE_NODE_TABLE.items()
        if owner_node_id == node_id
    ]
```

Update `stop_process()` so successful resources are removed independently and only by object identity:

```python
def stop_process(bridge_id: str, *, pop_traffic: bool = True) -> dict[str, int]:
    with PROCESS_LOCK:
        proxy = LOG_PROXY_TABLE.get(bridge_id)
        process = PROCESS_TABLE.get(bridge_id)
        owner_node_id = BRIDGE_NODE_TABLE.get(bridge_id)
    if proxy:
        proxy.stop()
        with PROCESS_LOCK:
            if LOG_PROXY_TABLE.get(bridge_id) is proxy:
                LOG_PROXY_TABLE.pop(bridge_id, None)
    if process:
        terminate_process(process)
        with PROCESS_LOCK:
            if PROCESS_TABLE.get(bridge_id) is process:
                PROCESS_TABLE.pop(bridge_id, None)
    with PROCESS_LOCK:
        if (
            bridge_id not in PROCESS_TABLE
            and bridge_id not in LOG_PROXY_TABLE
            and BRIDGE_NODE_TABLE.get(bridge_id) == owner_node_id
        ):
            BRIDGE_NODE_TABLE.pop(bridge_id, None)
    if pop_traffic:
        return pop_bridge_traffic(bridge_id)
    return bridge_traffic_snapshot(bridge_id)
```

This preserves traffic on any exception because the pop/snapshot is reached only after cleanup succeeds.

Extend lifecycle test isolation and the active-Bridge helper exactly as follows:

```python
# setUp
self._old_bridge_node_table = dict(app.BRIDGE_NODE_TABLE)
app.BRIDGE_NODE_TABLE.clear()

# tearDown
app.BRIDGE_NODE_TABLE.clear()
app.BRIDGE_NODE_TABLE.update(self._old_bridge_node_table)

# _activate_bridge
app.BRIDGE_NODE_TABLE[bridge_id] = str(bridge["node_id"])
```

- [ ] **Step 4: Verify GREEN and lifecycle regression coverage**

Run the focused command from Step 2, then:

```bash
python3 -m unittest tests.test_lifecycle_regressions -v
```

Expected: the process-only residual test and all lifecycle tests pass.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_lifecycle_regressions.py
git commit -m "fix: track bridge runtime ownership"
```

### Task 3: Drain proxy client handlers before persisting final traffic

**Files:**
- Modify: `app.py:2505-2580`
- Test: `tests/test_lifecycle_regressions.py`

**Interfaces:**
- Consumes: `Socks5LogProxy.stop()`, `stop_process(pop_traffic=False)`, `shutdown()`.
- Produces: tracked client sockets and handler threads; successful `stop()` guarantees no handler can call `add_bridge_traffic()` afterward.

- [ ] **Step 1: Add a failing full-shutdown drain test without binding a port**

Build a fake acceptor and patch only the client handler body, while using the real `_serve()`, `stop_process()`, and `shutdown()`:

```python
def test_shutdown_waits_for_proxy_clients_before_persisting_traffic(self) -> None:
    node_id = "node-final-traffic"
    bridge_id = "bridge-final-traffic"
    bridge = self._bridge(bridge_id, node_id, 4830, 24520)
    self._write_state(node_id, bridges=[bridge])
    app.PROCESS_TABLE[bridge_id] = FakeProcess(4830)
    app.BRIDGE_NODE_TABLE[bridge_id] = node_id
    app.init_bridge_traffic(bridge_id)

    entered = threading.Event()
    release = threading.Event()
    stop_done = threading.Event()
    errors: list[BaseException] = []

    class FakeClient:
        def shutdown(self, _how: int) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeServer:
        def __init__(self) -> None:
            self.accepted = False
            self.closed = threading.Event()

        def accept(self) -> tuple[FakeClient, tuple[str, int]]:
            if not self.accepted:
                self.accepted = True
                return FakeClient(), ("127.0.0.1", 50000)
            self.closed.wait(timeout=2)
            raise OSError("closed")

        def close(self) -> None:
            self.closed.set()

    proxy = app.Socks5LogProxy(
        bridge_id=bridge_id,
        node_id=node_id,
        node_name="Lifecycle Node",
        listen_port=24520,
        upstream_port=25520,
    )
    proxy._server = FakeServer()

    def handler(_client: FakeClient, _address: tuple[str, int]) -> None:
        entered.set()
        release.wait(timeout=2)
        app.add_bridge_traffic(bridge_id, "download_bytes", 19)

    with mock.patch.object(proxy, "_handle_client", side_effect=handler):
        proxy._thread = threading.Thread(target=proxy._serve, daemon=True)
        proxy._thread.start()
        self.assertTrue(entered.wait(timeout=1))
        app.LOG_PROXY_TABLE[bridge_id] = proxy

        def run_shutdown() -> None:
            try:
                with mock.patch.object(app, "terminate_process", return_value=None):
                    app.shutdown()
            except BaseException as error:
                errors.append(error)
            finally:
                stop_done.set()

        worker = threading.Thread(target=run_shutdown)
        worker.start()
        try:
            self.assertFalse(stop_done.wait(timeout=0.1))
        finally:
            release.set()
        worker.join(timeout=2)

    self.assertFalse(worker.is_alive())
    self.assertEqual(errors, [])
    saved = app.load_state()["bridges"][0]
    self.assertEqual(saved["download_bytes"], 19)
    self.assertEqual(saved["total_bytes"], 19)
    self.assertEqual(app.TRAFFIC_TABLE, {})
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_lifecycle_regressions.LifecycleRegressionTests.test_shutdown_waits_for_proxy_clients_before_persisting_traffic -v
```

Expected: `shutdown()` returns before `release` is set or persists zero bytes because the current proxy tracks only its accept thread.

- [ ] **Step 3: Track client sockets and handler threads**

Initialize lifecycle collections in `Socks5LogProxy.__init__()`:

```python
self._client_lock = threading.RLock()
self._client_sockets: set[socket.socket] = set()
self._client_threads: set[threading.Thread] = set()
```

Add a wrapper that owns handler deregistration:

```python
def _run_client_handler(self, client: socket.socket, address: tuple[str, int]) -> None:
    thread = threading.current_thread()
    try:
        self._handle_client(client, address)
    finally:
        with self._client_lock:
            self._client_sockets.discard(client)
            self._client_threads.discard(thread)
```

Replace the untracked creation in `_serve()` with registration before `start()`:

```python
thread = threading.Thread(
    target=self._run_client_handler,
    args=(client, address),
    daemon=True,
)
with self._client_lock:
    if self._stop.is_set():
        client.close()
        break
    self._client_sockets.add(client)
    self._client_threads.add(thread)
try:
    thread.start()
except BaseException:
    with self._client_lock:
        self._client_sockets.discard(client)
        self._client_threads.discard(thread)
    client.close()
    raise
```

Update `stop()` to close inputs, then drain all registered execution:

```python
def stop(self) -> None:
    self._stop.set()
    server = self._server
    accept_thread = self._thread
    if server:
        try:
            server.close()
        except OSError:
            pass
    with self._client_lock:
        clients = list(self._client_sockets)
    for client in clients:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass
    current = threading.current_thread()
    if accept_thread and accept_thread.is_alive() and accept_thread is not current:
        accept_thread.join(timeout=1.0)
    deadline = time.monotonic() + 3.0
    while True:
        with self._client_lock:
            handlers = [
                thread
                for thread in self._client_threads
                if thread is not current and thread.is_alive()
            ]
        if not handlers:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for thread in handlers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    with self._client_lock:
        lingering = [thread for thread in self._client_threads if thread.is_alive()]
    if (accept_thread and accept_thread.is_alive()) or lingering:
        raise RuntimeError("本地代理连接未能及时停止")
    self._server = None
    self._thread = None
```

On timeout the live thread references remain registered and `stop_process()` leaves the proxy in `LOG_PROXY_TABLE`, so a later shutdown can retry.

- [ ] **Step 4: Verify GREEN, then run focused and full Python tests**

Run the focused command from Step 2, then:

```bash
python3 -m unittest tests.test_lifecycle_regressions -v
python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/proxybridge-pycache python3 -m py_compile app.py tests/*.py
git diff --check
```

Expected: the drain test passes, all Python tests pass except any pre-existing environment skips, compilation succeeds, and the diff check is clean.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_lifecycle_regressions.py
git commit -m "fix: drain proxy clients before shutdown"
```
