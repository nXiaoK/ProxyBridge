# WebDAV 同步逻辑全局审查

**审查范围**: `webdav_sync.py` + `app.py` 中的同步调用点  
**审查日期**: 2024-01-09  
**严重级别**: 2 个 CRITICAL + 3 个 HIGH + 5 个 MEDIUM/LOW

---

## 发现的问题

### 1. 🔴 CRITICAL: 状态变更检测的竞态条件

**位置**: `webdav_sync.py:2091-2099` + `app.py:4337`

**问题**:
```python
def mark_local_change(self) -> None:
    with self.state_lock:
        state = self.load_state()
        cfg = self.get_settings(state)
        cfg["last_local_change_at"] = int(time.time())
        cfg["last_applied_state_sha256"] = ""  # ❌ 清除标记
        state["settings"]["webdav"] = cfg
        self.save_state(state)
```

**危害**:
- 本地修改发生在多处（标签变更、订阅删除、浏览器删除等）
- `mark_local_change()` 异步调用清除 `last_applied_state_sha256`
- **中间存在时间窗口**: 自动sync线程可能读到旧标记，误判为"无本地变更"
- **后果**: 本地修改被远端覆盖，**数据丢失**

**场景演示**:
```
1. 用户修改浏览器标签 (app.py:1527)
2. app 调用 get_sync_engine().maybe_auto_push(reason="更新浏览器标签")
3. auto-sync 线程读状态，此时 last_applied_state_sha256 可能还未清除
4. auto-sync 认为本地==远端，跳过push
5. mark_local_change() 清除标记（太晚了）
6. Pull 时: 本地修改彻底丢失
```

**修复建议**: 
- 将 `mark_local_change()` 的调用**前置**到修改发生时
- 或使用 **变更版本号** 而不是清除标记

---

### 2. 🔴 CRITICAL: Manifest 原子性破坏 (Profile 可能悬空)

**位置**: `webdav_sync.py:1500-1660`

**问题**:
```python
# Push 逻辑顺序:
for instance in instances:
    # 1. 上传 Profile 到带时间戳的文件
    profile_filename = f"{instance_id}-{digest[:16]}-{snapshot_id}.tar.gz"
    profile_url = client.url_for(cfg["remote_root"], PROFILES_DIR_NAME, profile_filename)
    client.put_bytes(profile_url, blob)  # 2024-01-09-12:34:56-xxx.tar.gz
    
# 2. 最后上传 Manifest (指向新Profile)
# 3. 然后删除旧的 alias

# ❌ 风险: 如果步骤1和2之间发生 Pull:
#    - Pull 读到旧 Manifest，指向 2024-01-08-... 版本
#    - 该版本在步骤3中被删除
#    - Profile 下载失败: "远端缺少 Profile 包"
```

**危害**:
- **Pull 失败**: 另一设备无法同步，一直卡在"缺少Profile"
- **用户困惑**: 明明看到远端有更新，却无法拉取

**修复建议**:
1. 先上传新 Profile
2. 先上传新 Manifest（指向新 Profile）
3. **然后才能**删除旧 Profile（确保 Manifest-Profile 一致性）
4. 实现 **two-phase commit** 或引入 `.staging` 目录

---

### 3. 🟠 HIGH: 自动同步队列的优先级倒置

**位置**: `webdav_sync.py:2112-2149`

**问题**:
```python
def _enqueue_auto_sync(self, *, full_push: bool = False, instance_id: str = "", reason: str = "") -> None:
    with self._auto_queue_lock:
        if full_push:
            self._auto_full_push_pending = True
            self._auto_instance_ids.clear()  # ❌ 丢弃 instance_ids
```

**场景**:
```
1. 浏览器A停止 → _enqueue_auto_sync(instance_id="A")
2. 浏览器B停止 → _enqueue_auto_sync(instance_id="B")  # 队列: {A, B}
3. 删除订阅 → _enqueue_auto_sync(full_push=True)      # 队列: {full_push}
4. full_push 完成
5. 浏览器A、B 的最近上传永远丢失！
```

**危害**:
- **Profile 上传丢失**: A、B 的最新状态无法上传
- **跨设备不同步**: 其他设备拉不到最新Profile

**修复建议**:
- 优先处理 instance_ids，然后执行 full_push
- 或保留 instance_ids，在 full_push 后执行

---

### 4. 🟠 HIGH: Pull 状态指纹不完整

**位置**: `webdav_sync.py:1334-1348` 

**问题**:
```python
def _local_state_fingerprint(self, state: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "subscriptions": state.get("subscriptions") or [],
        "nodes": state.get("nodes") or [],
    }
    if self.sync_browser_data:
        payload["browser_instances"] = state.get("browser_instances") or []
    # ❌ 未包含:
    #    - browser_instances 的运行状态 (running/stopped)
    #    - 最后修改时间
    #    - Profile 相关元数据
```

**危害**:
```
1. Pull 开始，计算指纹 FP1 (instance A: running)
2. 自动上传线程同时上传 instance A 的 Profile
3. Pull 途中下载完 state 和 profiles
4. Pull 验证指纹：FP1 == FP2 (instance A 停止后的指纹)
5. 指纹匹配，但实际数据已变 → 应用了错误的远端快照
```

**修复建议**:
- 指纹应包含 browser_instances 的完整 metadata
- 或使用版本号/timestamp 替代指纹

---

### 5. 🟠 HIGH: Auto-upload 线程的死锁风险

**位置**: `webdav_sync.py:2151-2175`

**问题**:
```python
def _drain_auto_sync_queue(self) -> None:
    while True:
        job = self._take_auto_sync_job()
        if job is None:
            return
        full_push, instance_ids, reasons = job
        try:
            with self.state_lock:  # ❌ 如果死锁在这里
                state = self.load_state()
                cfg = self.get_settings(state)
                # ... 
            if full_push:
                self.push()  # 再次获取 state_lock
```

**危害**:
- Daemon 线程卡住，无法处理后续自动同步请求
- 用户感觉同步"冻结"了
- **无明确的超时或死锁检测**

**修复建议**:
- 使用 lock timeout（Python 3.10+ 支持）
- 或将 state_lock 的持有时间最小化
- 添加 watchdog 检测 daemon 线程冻结

---

### 6. 🟡 MEDIUM: Pull 中删除本地 Profile 的顺序错误

**位置**: `webdav_sync.py:1970-2000`

**问题**:
```python
for record in (applied.get("removed_browsers") or []):
    try:
        path = self._safe_profile_path(...)
        if path.exists():
            shutil.rmtree(path, ignore_errors=False)  # 立即删除
            purged_profiles.append(iid)
    except Exception as exc:
        errors.append(f"清理本地 Profile {iid}: {exc}")

if errors:
    raise RuntimeError("; ".join(errors))

# ❌ 此时本地已删除部分 Profile，但同步标记未更新
# 如果异常，状态不一致
```

**危害**:
- 删除 Profile 1,2 成功，删除 3 失败
- 本地已丢失 1,2，但 `last_applied_state_sha256` 未更新
- 下次 pull：本地指纹错误，无法判断是否需要再同步

**修复建议**:
- 先收集所有待删除 Profile
- 验证删除不会失败，再执行删除
- 使用 **回滚点** 或 **临时标记**

---

### 7. 🟡 MEDIUM: 加密状态转换无原子性保证

**位置**: `webdav_sync.py:1296-1306`

**问题**:
```python
def _encode_blob(self, data: bytes, cfg: dict[str, Any]) -> bytes:
    if cfg.get("encrypt"):  # ❌ cfg 可能在多线程中改变
        return encrypt_payload(data, str(cfg.get("encrypt_password") or ""))
    return data

def _decode_blob(self, data: bytes, cfg: dict[str, Any]) -> bytes:
    # ...
    if cfg.get("encrypt"):  # ❌ 可能与编码时的 encrypt 状态不同
        return decrypt_payload(data, str(cfg.get("encrypt_password") or ""))
```

**场景**:
```
1. Push: 读 cfg, encrypt=True → 上传加密数据
2. 中间: 用户禁用加密 → cfg["encrypt"] = False
3. Pull: 读 cfg, encrypt=False → 尝试直接解析加密数据
4. 错误: "远端数据已加密，但配置禁用加密"
```

**修复建议**:
- 在 push/pull 开始时 **snapshot cfg**
- 不要在操作中途改变 encrypt 设置
- 使用配置版本号

---

### 8. 🟡 MEDIUM: Subscription-Only 模式的 device_id 污染

**位置**: `webdav_sync.py:1562-1599`

**问题**:
当 `sync_browser_data=False`（只同步订阅）时：
```python
remote_snapshot = remote_state or {}
remote_browsers = [...]  # 保留远端的浏览器
local_nodes = [...]      # 只同步本地订阅节点

sync_state = build_sync_state(
    state,
    device_id=str(cfg["device_id"]),  # ❌ 用本地 device_id
    profile_hashes=dict(remote_hashes),
)
sync_state["nodes"] = local_nodes + preserved_nodes
sync_state["browser_instances"] = remote_browsers
```

**危害**:
- `device_id` = 本地设备ID，但 `browser_instances` = 远端设备数据
- LWW 冲突检测错误: 无法判断是否是本设备上次修改
- **跨设备冲突判断混乱**

**修复建议**:
- 当 subscription-only 时，**保留远端 device_id**
- 或在 metadata 中分离标记源设备

---

### 9. 🟢 LOW: Manifest 和 State 的一致性检查不足

**位置**: `webdav_sync.py:1460-1477`, `2005-2006`

**问题**:
- Push 时 Manifest 通过 MOVE 上传（通常原子性较好）
- 但 MOVE 失败时，旧 Manifest 仍存在，指向可能不一致的数据
- 本地已更新 `last_applied_state_sha256`，无法检测远端不一致

**修复建议**:
- 实现 **manifest version** 跟踪
- Pull 时验证 manifest <-> state <-> profile 三方一致性

---

### 10. 🟢 LOW: Profile 下载无重试机制

**位置**: `webdav_sync.py:1888-1911`

**问题**:
```python
blob = self._decode_blob(
    client.get_bytes(url, max_bytes=MAX_PROFILE_ARCHIVE_BYTES + 1024 * 1024), cfg
)
# ❌ 网络中断 → 超时异常
# ❌ 无重试 → Pull 失败
```

**危害**:
- 大 Profile (500MB+) 下载中途中断
- 不会自动重试，用户必须手动重新 pull
- 网络不稳定环境体验差

**修复建议**:
- 实现指数退避重试（3次）
- 断点续传或分块下载

---

## 风险矩阵

| 问题 | 严重 | 频率 | 影响范围 | 修复工作量 |
|------|------|------|---------|----------|
| #1 竞态条件 | 🔴 | ⭐⭐⭐ | 状态丢失 | 中 |
| #2 Manifest原子性 | 🔴 | ⭐⭐ | Pull失败 | 高 |
| #3 队列倒置 | 🟠 | ⭐⭐ | 上传丢失 | 低 |
| #4 指纹不完整 | 🟠 | ⭐ | 数据混乱 | 中 |
| #5 死锁风险 | 🟠 | ⭐⭐ | 冻结 | 中 |
| #6 删除顺序 | 🟡 | ⭐ | 不一致 | 低 |
| #7 加密转换 | 🟡 | ⭐ | 解密失败 | 低 |
| #8 device_id污染 | 🟡 | ⭐ | 冲突判断错 | 中 |
| #9 一致性检查 | 🟢 | ★ | 数据污染 | 低 |
| #10 无重试 | 🟢 | ⭐⭐⭐ | UX差 | 低 |

---

## 修复优先级

### Phase 1 (立即修复 - 防止数据丢失)
1. ✅ **#1 竞态条件**: 使用变更版本号替代清除标记
2. ✅ **#2 Manifest原子性**: 实现三阶段提交 (新Profile → 新Manifest → 删除旧)

### Phase 2 (高优先级 - 稳定性)
3. ✅ **#3 队列倒置**: 保留instance_ids，优先处理
4. ✅ **#4 指纹不完整**: 包含完整metadata
5. ✅ **#5 死锁风险**: 添加timeout和watchdog

### Phase 3 (中优先级 - 数据一致性)
6. ✅ **#6 删除顺序**: 使用回滚点
7. ✅ **#8 device_id污染**: 分离源设备标记

### Phase 4 (优化 - 提升体验)
8. ✅ **#7 加密转换**: Snapshot配置
9. ✅ **#9 一致性检查**: Manifest版本跟踪
10. ✅ **#10 无重试**: 指数退避重试

---

## 建议的测试场景

```gherkin
# 测试场景 1: 并发修改
Given 设备A启用auto_push
And 设备B执行pull
When 设备A修改标签 AND 删除订阅 (并发)
Then 最终状态应一致 AND 无数据丢失

# 测试场景 2: 中断恢复
Given push正在上传Profile
When 网络中断
Then pull应失败 OR 重试成功

# 测试场景 3: 多个浏览器
Given 5个浏览器相继停止
When 自动上传 AND 用户同时删除订阅
Then 所有Profile应上传 AND 订阅应同步
```

