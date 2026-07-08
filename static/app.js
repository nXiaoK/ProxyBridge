const appState = {
  cores: {},
  chrome: null,
  fingerprint_chromium: null,
  active_browsers: [],
  subscriptions: [],
  nodes: [],
  bridges: [],
  tests: {},
  requestLogs: [],
  systemLogs: [],
  activeView: "nodes",
  filters: {
    protocol: "all",
    region: "all",
    search: "",
  },
  logFilters: {
    search: "",
    node: "all",
    status: "all",
  },
  systemLogFilters: {
    search: "",
    level: "all",
  },
};

const $ = (selector) => document.querySelector(selector);
let lastCoreLogTrigger = null;

// 侧边栏可切换的视图列表,顺序与 HTML 中 .nav-item / .view 一致。
const VIEW_IDS = ["nodes", "subscriptions", "bridges", "logs", "system"];

// 切换当前视图:高亮对应侧边栏项、显示对应内容区,并记住上次选择。
function setActiveView(view) {
  if (!VIEW_IDS.includes(view)) view = "nodes";
  appState.activeView = view;
  try { localStorage.setItem("pb-view", view); } catch (e) {}
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.setAttribute("aria-current", item.dataset.view === view ? "true" : "false");
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });
}

// 更新单个侧边栏计数徽标;为 0 时通过 data-zero 隐藏,避免视觉噪音。
function setNavCount(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = String(value);
  el.dataset.zero = value ? "false" : "true";
}

// 刷新四个视图的侧边栏计数徽标。
function renderNav() {
  setNavCount("navCountNodes", appState.nodes.length);
  setNavCount("navCountSubs", appState.subscriptions.length);
  setNavCount("navCountBridges", runningBridges().length);
  setNavCount("navCountLogs", (appState.requestLogs || []).length);
  setNavCount("navCountSystem", (appState.systemLogs || []).length);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function icon(name) {
  const paths = {
    play: '<path d="M8 5v14l11-7z"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    close: '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
    copy: '<path d="M8 8h10v10H8z"/><path d="M6 16H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    stop: '<path d="M7 7h10v10H7z"/>',
    refresh: '<path d="M20 12a8 8 0 0 1-13.7 5.6M4 12A8 8 0 0 1 17.7 6.4M18 3v4h-4M6 21v-4h4"/>',
    browser: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 8h18"/><path d="M8 4v4"/>',
    trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 15h10l1-15"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.1 0l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.1 0l-2 2A5 5 0 0 0 12 20.1l1.1-1.1"/>',
    minus: '<path d="M5 12h14"/>',
    activity: '<path d="M22 12h-4l-3 8L9 4l-3 8H2"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.play}</svg>`;
}

async function api(path, body = null) {
  const options = body
    ? {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    : {};
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function showToast(message, kind = "ok") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", kind === "error");
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 3600);
}

function formatTime(seconds) {
  if (!seconds) return "-";
  return new Date(seconds * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatLogTime(seconds) {
  if (!seconds) return "-";
  return new Date(seconds * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(kb >= 100 ? 0 : 1)} KB`;
  const mb = kb / 1024;
  return `${mb.toFixed(mb >= 100 ? 0 : 1)} MB`;
}

function statusLabel(status) {
  const labels = {
    connecting: "连接中",
    connected: "已连接",
    closed: "已关闭",
    failed: "失败",
  };
  return labels[status] || status || "-";
}

function initials(value) {
  const chars = Array.from(String(value || "PB")).filter((char) => /[a-z0-9\u4e00-\u9fff]/i.test(char));
  return chars.slice(0, 2).join("").toUpperCase() || "PB";
}

function runningBridges() {
  return appState.bridges.filter((bridge) => ["running", "starting"].includes(bridge.status));
}

function endpointParts(socksUrl, fallbackPort) {
  try {
    const url = new URL(socksUrl);
    return {
      address: url.hostname || "127.0.0.1",
      port: url.port || String(fallbackPort || "-"),
    };
  } catch {
    return {
      address: "127.0.0.1",
      port: String(fallbackPort || "-"),
    };
  }
}

async function loadStatus() {
  const status = await api("/api/status");
  appState.cores = status.cores || {};
  appState.chrome = status.chrome || null;
  appState.fingerprint_chromium = status.fingerprint_chromium || null;
  appState.active_browsers = status.active_browsers || [];
  appState.subscriptions = status.subscriptions || [];
  appState.nodes = status.nodes || [];
  appState.bridges = status.bridges || [];
  appState.tests = status.tests || {};
  appState.requestLogs = status.request_logs || [];
  appState.systemLogs = status.system_logs || [];
  render();
}

function render() {
  renderNav();
  renderCores();
  renderSubscriptions();
  renderFilters();
  renderNodes();
  renderBridges();
  renderLogFilters();
  renderRequestLogs();
  renderSystemLogs();
}

function renderCores() {
  const coreStrip = $("#coreStrip");
  const singBox = appState.cores["sing-box"];
  const xray = appState.cores.xray;
  const fpKernel = appState.fingerprint_chromium;
  // 指纹内核状态:已安装显示 ready,缺失显示可点击的「下载指纹内核」按钮。
  const fpPill = fpKernel
    ? `<button class="pill core-pill ok" type="button" data-action="download-kernel" aria-label="指纹内核已就绪，点击可重新下载" title="指纹内核已就绪：${escapeHtml(fpKernel)}">指纹内核 ready</button>`
    : `<button class="pill core-pill warn" type="button" data-action="download-kernel" aria-label="下载指纹内核 fingerprint-chromium" title="点击自动下载安装指纹内核 fingerprint-chromium">下载指纹内核</button>`;
  coreStrip.innerHTML = `
    <button class="pill core-pill ${singBox ? "ok" : "warn"}" type="button" data-action="show-core-log" data-core="sing-box" aria-label="查看 sing-box 运行日志" title="查看 sing-box 运行日志">sing-box ${singBox ? "ready" : "missing"}</button>
    <button class="pill core-pill ${xray ? "ok" : "warn"}" type="button" data-action="show-core-log" data-core="xray" aria-label="查看 xray 运行日志" title="查看 xray 运行日志">xray ${xray ? "ready" : "missing"}</button>
    ${fpPill}
  `;
}

function renderSubscriptions() {
  const list = $("#subscriptionList");
  const count = $("#subsCount");
  if (count) count.textContent = `${appState.subscriptions.length} 个订阅`;
  if (!appState.subscriptions.length) {
    list.innerHTML = '<div class="subscription-empty">暂无订阅，粘贴订阅 URL 或原始链接后即可导入。</div>';
    return;
  }
  list.innerHTML = appState.subscriptions
    .map(
      (item) => `
        <article class="subscription-item">
          <div class="subscription-head">
            <span class="sub-avatar">${escapeHtml(initials(item.name))}</span>
            <div>
              <strong class="subscription-name">${escapeHtml(item.name)}</strong>
              <div class="subscription-meta">${item.node_count || 0} 个节点 · ${formatTime(item.last_update)}</div>
            </div>
            <span class="sub-status">已启用</span>
          </div>
          <div class="item-actions">
            ${
              item.url
                ? `<button class="row-button" data-action="refresh-sub" data-id="${escapeHtml(item.id)}">${icon("refresh")}<span>刷新</span></button>`
                : ""
            }
            <button class="row-button danger" data-action="delete-sub" data-id="${escapeHtml(item.id)}">${icon("trash")}<span>删除</span></button>
          </div>
        </article>
      `
    )
    .join("");
}

function setSelectOptions(select, values, current, allLabel) {
  const normalized = ["all", ...values.filter(Boolean).sort((a, b) => a.localeCompare(b, "zh-CN"))];
  select.innerHTML = normalized
    .map((value) => `<option value="${escapeHtml(value)}">${value === "all" ? allLabel : escapeHtml(value)}</option>`)
    .join("");
  select.value = normalized.includes(current) ? current : "all";
}

function renderFilters() {
  const protocols = [...new Set(appState.nodes.map((node) => node.protocol))];
  const regions = [...new Set(appState.nodes.map((node) => node.region))];
  setSelectOptions($("#protocolFilter"), protocols, appState.filters.protocol, "全部协议");
  setSelectOptions($("#regionFilter"), regions, appState.filters.region, "全部地区");
  $("#searchInput").value = appState.filters.search;
}

function bridgeForNode(nodeId) {
  return appState.bridges.find((bridge) => bridge.node_id === nodeId && ["running", "starting"].includes(bridge.status));
}

function renderLogFilters() {
  const select = $("#logNodeFilter");
  const nodeOptions = new Map();
  for (const entry of appState.requestLogs || []) {
    const id = entry.node_id || entry.node_name || "";
    if (!id) continue;
    nodeOptions.set(id, entry.node_name || id);
  }
  const options = [...nodeOptions.entries()].sort((a, b) => a[1].localeCompare(b[1], "zh-CN"));
  select.innerHTML = [
    '<option value="all">全部节点</option>',
    ...options.map(([id, name]) => `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`),
  ].join("");
  if (![...nodeOptions.keys(), "all"].includes(appState.logFilters.node)) {
    appState.logFilters.node = "all";
  }
  select.value = appState.logFilters.node;
  $("#logStatusFilter").value = ["all", "connecting", "connected", "closed", "failed"].includes(appState.logFilters.status)
    ? appState.logFilters.status
    : "all";
  $("#logSearchInput").value = appState.logFilters.search;
}

function filteredRequestLogs() {
  const search = appState.logFilters.search.trim().toLowerCase();
  return (appState.requestLogs || []).filter((entry) => {
    if (appState.logFilters.node !== "all") {
      const id = entry.node_id || entry.node_name || "";
      if (id !== appState.logFilters.node) return false;
    }
    if (appState.logFilters.status !== "all" && entry.status !== appState.logFilters.status) return false;
    if (!search) return true;
    const target = entry.target || `${entry.target_host || ""}:${entry.target_port || ""}`;
    return [
      target,
      entry.target_host,
      entry.target_port,
      entry.node_name,
      entry.client,
      entry.status,
      statusLabel(entry.status),
      entry.message,
      entry.command,
      entry.proxy_protocol,
    ].some((value) => String(value || "").toLowerCase().includes(search));
  });
}

function testSummary(nodeId) {
  const result = appState.tests[nodeId];
  if (!result) {
    return `
      <div class="test-result">
        <span class="test-line">未测试</span>
        <button class="row-button" data-action="test" data-node="${escapeHtml(nodeId)}"><span>测试</span></button>
      </div>
    `;
  }
  const ok = Boolean(result.ok);
  const latency = result.latency_ms == null ? "-" : `${result.latency_ms} ms`;
  return `
    <div class="test-result">
      <span class="test-line ${ok ? "ok" : "bad"}">${ok ? latency : "失败"}</span>
      <span class="test-message">${escapeHtml(result.message || "")}</span>
      <button class="row-button" data-action="test" data-node="${escapeHtml(nodeId)}"><span>重测</span></button>
    </div>
  `;
}

function filteredNodes() {
  const search = appState.filters.search.trim().toLowerCase();
  return appState.nodes.filter((node) => {
    if (appState.filters.protocol !== "all" && node.protocol !== appState.filters.protocol) return false;
    if (appState.filters.region !== "all" && node.region !== appState.filters.region) return false;
    if (!search) return true;
    return [node.name, node.server, node.source, node.protocol, node.region].some((value) =>
      String(value || "").toLowerCase().includes(search)
    );
  });
}

function renderNodes() {
  const rows = $("#nodeRows");
  const empty = $("#emptyNodes");
  const nodes = filteredNodes();
  $("#nodeCount").textContent = `${nodes.length} / ${appState.nodes.length} 个节点`;
  empty.classList.toggle("visible", appState.nodes.length === 0);
  if (!nodes.length) {
    rows.innerHTML = "";
    if (appState.nodes.length) {
      empty.textContent = "没有匹配的节点";
      empty.classList.add("visible");
    } else {
      empty.textContent = "暂无节点";
    }
    return;
  }
  empty.classList.remove("visible");
  rows.innerHTML = nodes
    .map((node) => {
      const bridge = bridgeForNode(node.id);
      const browserActive = appState.active_browsers.includes(node.id);
      const transport = node.transport || "-";
      const security = node.security || "";
      const server = `${node.server}:${node.port}`;
      return `
        <tr class="${bridge ? "is-running" : ""}">
          <td>
            <div class="name-cell">
              <span class="node-name">${escapeHtml(node.name)}</span>
              <span class="node-source">${escapeHtml(node.source || "-")}</span>
            </div>
          </td>
          <td><span class="region-cell">${escapeHtml(node.region || "未分类")}</span></td>
          <td><span class="tag protocol-tag">${escapeHtml(node.protocol)}</span></td>
          <td><span class="transport-tag"><strong>${escapeHtml(transport)}</strong><em>${escapeHtml(security)}</em></span></td>
          <td><span class="mono server-cell" title="${escapeHtml(server)}">${escapeHtml(server)}</span></td>
          <td><span class="tag core-tag">${escapeHtml(node.preferred_core || "sing-box")}</span></td>
          <td>${testSummary(node.id)}</td>
          <td>
            <div class="row-actions">
              ${
                bridge
                  ? `<button class="row-button" data-action="copy" data-text="${escapeHtml(bridge.socks_url)}">${icon("copy")}<span>SOCKS</span></button>
                    <button class="row-button" data-action="copy" data-text="${escapeHtml(bridge.http_url || bridge.socks_url)}">${icon("copy")}<span>HTTP</span></button>`
                  : `<button class="row-button primary-row" data-action="start" data-node="${escapeHtml(node.id)}">${icon("play")}<span>启动并复制</span></button>`
              }
              <button class="row-button" data-action="launch-browser" data-node="${escapeHtml(node.id)}" aria-label="用独立指纹浏览器启动 ${escapeHtml(node.name)}" title="用独立指纹浏览器启动：伪造指纹 + 独立 Profile + 绑定该节点代理">${icon("browser")}<span>浏览器</span></button>
              ${
                browserActive
                  ? `<button class="row-button danger" data-action="stop-browser" data-node="${escapeHtml(node.id)}" aria-label="停止 ${escapeHtml(node.name)} 的浏览器和本地代理" title="结束该节点的指纹浏览器进程，并停止其本地代理">${icon("stop")}<span>停止浏览器</span></button>`
                  : ""
              }
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

function renderBridges() {
  const list = $("#bridgeList");
  const bridges = runningBridges();
  const count = $("#bridgesCount");
  if (count) count.textContent = `${bridges.length} 个运行中`;
  $("#stopAll").disabled = bridges.length === 0;

  if (!bridges.length) {
    list.innerHTML = `
      <div class="bridge-empty">
        <div class="status-orb idle">${icon("minus")}</div>
        <strong>暂无运行中的本地代理</strong>
        <span>在「节点」里启动一个节点后，它会在这里显示为一张独立的端口卡片。同一端口同时支持 SOCKS5、HTTP 代理和 HTTPS CONNECT。</span>
      </div>
    `;
    return;
  }

  list.innerHTML = bridges.map((bridge) => {
    const endpoint = endpointParts(bridge.socks_url, bridge.port);
    const httpUrl = bridge.http_url || `http://127.0.0.1:${bridge.port}`;
    const uploadBytes = Number(bridge.upload_bytes || 0);
    const downloadBytes = Number(bridge.download_bytes || 0);
    const totalBytes = Number(bridge.total_bytes || uploadBytes + downloadBytes);
    return `
      <article class="bridge-card" data-bridge-id="${escapeHtml(bridge.id)}">
        <header class="bridge-card-head">
          <div class="status-orb ${bridge.status === "running" ? "" : "idle"}">${icon(bridge.status === "running" ? "check" : "minus")}</div>
          <div class="bridge-card-title">
            <div class="bridge-card-name">
              <strong>${escapeHtml(bridge.node_name)}</strong>
              <span class="status ${escapeHtml(bridge.status)}">${escapeHtml(bridge.status)}</span>
            </div>
            <div class="bridge-meta">${escapeHtml(bridge.region || "-")} · ${escapeHtml(bridge.protocol || "-")} · ${escapeHtml(bridge.core || "-")}</div>
          </div>
        </header>
        <div class="traffic-strip" aria-label="本地代理流量统计">
          <div class="traffic-metric">
            <span class="traffic-label">上行</span>
            <strong class="traffic-value">${escapeHtml(formatBytes(uploadBytes))}</strong>
          </div>
          <div class="traffic-metric">
            <span class="traffic-label">下行</span>
            <strong class="traffic-value">${escapeHtml(formatBytes(downloadBytes))}</strong>
          </div>
          <div class="traffic-metric total">
            <span class="traffic-label">合计</span>
            <strong class="traffic-value">${escapeHtml(formatBytes(totalBytes))}</strong>
          </div>
        </div>
        <div class="endpoint-card">
          <div class="endpoint-row">
            <div>
              <span class="endpoint-label">本地端口</span>
              <span class="endpoint-value">${escapeHtml(endpoint.port)}</span>
            </div>
            <button class="row-button copy-icon" data-action="copy" data-text="${escapeHtml(endpoint.port)}" aria-label="复制本地端口" title="复制本地端口">${icon("copy")}</button>
          </div>
          <div class="endpoint-row">
            <div>
              <span class="endpoint-label">监听地址</span>
              <span class="endpoint-value">${escapeHtml(endpoint.address)}</span>
            </div>
            <button class="row-button copy-icon" data-action="copy" data-text="${escapeHtml(endpoint.address)}" aria-label="复制监听地址" title="复制监听地址">${icon("copy")}</button>
          </div>
          <div class="endpoint-row endpoint-url">
            <div>
              <span class="endpoint-label">SOCKS5 URL</span>
              <span class="endpoint-value mono" title="${escapeHtml(bridge.socks_url)}">${escapeHtml(bridge.socks_url)}</span>
            </div>
            <button class="row-button copy-icon" data-action="copy" data-text="${escapeHtml(bridge.socks_url)}" aria-label="复制 SOCKS5 地址" title="复制 SOCKS5 地址">${icon("copy")}</button>
          </div>
          <div class="endpoint-row endpoint-url">
            <div>
              <span class="endpoint-label">HTTP / HTTPS 代理 URL</span>
              <span class="endpoint-value mono" title="${escapeHtml(httpUrl)}">${escapeHtml(httpUrl)}</span>
            </div>
            <button class="row-button copy-icon" data-action="copy" data-text="${escapeHtml(httpUrl)}" aria-label="复制 HTTP/HTTPS 代理地址" title="复制 HTTP/HTTPS 代理地址">${icon("copy")}</button>
          </div>
        </div>
        <div class="bridge-actions">
          <button class="row-button danger" data-action="stop" data-id="${escapeHtml(bridge.id)}">${icon("stop")}<span>停止</span></button>
          <button class="row-button" data-action="restart" data-id="${escapeHtml(bridge.id)}" data-node="${escapeHtml(bridge.node_id)}">${icon("refresh")}<span>重启</span></button>
        </div>
      </article>
    `;
  }).join("");
}

function renderRequestLogs() {
  const rows = $("#logRows");
  const empty = $("#emptyLogs");
  const allLogs = appState.requestLogs || [];
  const logs = filteredRequestLogs();
  $("#logCount").textContent = allLogs.length === logs.length ? `${logs.length} 条` : `${logs.length} / ${allLogs.length} 条`;
  if (!logs.length) {
    rows.innerHTML = "";
    empty.textContent = allLogs.length ? "没有匹配的连接日志" : "暂无连接日志";
    empty.classList.add("visible");
    return;
  }
  empty.classList.remove("visible");
  rows.innerHTML = logs
    .map((entry) => {
      const status = entry.status || "-";
      const upload = formatBytes(entry.upload_bytes);
      const download = formatBytes(entry.download_bytes);
      const duration = entry.duration_ms == null ? "-" : `${entry.duration_ms} ms`;
      const target = entry.target || `${entry.target_host || "-"}:${entry.target_port || "-"}`;
      const protocol = entry.proxy_protocol ? String(entry.proxy_protocol).toUpperCase() : "";
      const command = entry.command ? String(entry.command).toUpperCase() : "";
      const meta = [protocol, command].filter(Boolean).join(" · ");
      return `
        <tr>
          <td><span class="mono">${escapeHtml(formatLogTime(entry.started_at))}</span></td>
          <td>
            <div class="log-target">
              <span>${escapeHtml(target)}</span>
              ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
              ${entry.message ? `<small>${escapeHtml(entry.message)}</small>` : ""}
            </div>
          </td>
          <td><span class="log-node">${escapeHtml(entry.node_name || "-")}</span></td>
          <td><span class="mono">${escapeHtml(entry.client || "-")}</span></td>
          <td><span class="log-status ${escapeHtml(status)}">${escapeHtml(statusLabel(status))}</span></td>
          <td>
            <div class="log-metrics">
              <span>${escapeHtml(upload)} ↑</span>
              <span>${escapeHtml(download)} ↓</span>
              <span>${escapeHtml(duration)}</span>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

// 系统日志级别的中文标签。
function systemLevelLabel(level) {
  const labels = { info: "信息", warn: "警告", error: "错误" };
  return labels[level] || level || "信息";
}

// 按当前筛选(级别 + 关键词)过滤系统日志。
function filteredSystemLogs() {
  const search = appState.systemLogFilters.search.trim().toLowerCase();
  const level = appState.systemLogFilters.level;
  return (appState.systemLogs || []).filter((entry) => {
    if (level !== "all" && entry.level !== level) return false;
    if (!search) return true;
    return [entry.message, entry.category, entry.level, systemLevelLabel(entry.level)]
      .some((value) => String(value || "").toLowerCase().includes(search));
  });
}

// 渲染系统日志表格,并同步筛选控件的当前值与计数。
function renderSystemLogs() {
  const rows = $("#systemLogRows");
  const empty = $("#emptySystemLogs");
  if (!rows) return;
  // 回填筛选控件的值(轮询刷新时保持用户选择)。
  $("#systemSearchInput").value = appState.systemLogFilters.search;
  $("#systemLevelFilter").value = ["all", "info", "warn", "error"].includes(appState.systemLogFilters.level)
    ? appState.systemLogFilters.level
    : "all";

  const all = appState.systemLogs || [];
  const logs = filteredSystemLogs();
  $("#systemCount").textContent = all.length === logs.length ? `${logs.length} 条` : `${logs.length} / ${all.length} 条`;
  if (!logs.length) {
    rows.innerHTML = "";
    empty.textContent = all.length ? "没有匹配的系统日志" : "暂无系统日志";
    empty.classList.add("visible");
    return;
  }
  empty.classList.remove("visible");
  rows.innerHTML = logs
    .map((entry) => {
      const level = entry.level || "info";
      return `
        <tr>
          <td><span class="mono">${escapeHtml(formatLogTime(entry.ts))}</span></td>
          <td><span class="log-level ${escapeHtml(level)}">${escapeHtml(systemLevelLabel(level))}</span></td>
          <td><span class="log-category">${escapeHtml(entry.category || "-")}</span></td>
          <td><span class="log-message">${escapeHtml(entry.message || "")}</span></td>
        </tr>
      `;
    })
    .join("");
}

function closeCoreLogModal() {
  $("#coreLogModal").hidden = true;
  document.body.classList.remove("modal-open");
  if (lastCoreLogTrigger?.isConnected) {
    lastCoreLogTrigger.focus();
  }
  lastCoreLogTrigger = null;
}

function renderCoreLogPayload(payload) {
  const core = payload.core || "core";
  const corePath = payload.core_path || "missing";
  const entries = payload.entries || [];
  const metaParts = [`${entries.length} 个日志片段`];
  if (payload.hidden_empty_count) metaParts.push(`已隐藏 ${payload.hidden_empty_count} 个空历史记录`);
  if (payload.truncated_count) metaParts.push(`还有 ${payload.truncated_count} 个未显示`);
  $("#coreLogTitle").textContent = `${core} 运行日志`;
  $("#coreLogMeta").textContent = `${corePath} · ${metaParts.join(" · ")}`;

  const body = $("#coreLogBody");
  if (!entries.length) {
    body.innerHTML = `
      <div class="core-log-empty">
        <strong>暂无运行日志</strong>
        <span>启动使用 ${escapeHtml(core)} 的节点后，这里会显示对应核心输出；无输出的已停止历史记录不会占用列表。</span>
      </div>
    `;
    return;
  }

  body.innerHTML = entries
    .map((entry) => {
      const status = entry.status || "-";
      const title = `${entry.node_name || "未知节点"} · ${entry.port || "-"}`;
      const log = String(entry.log || "").trim();
      const logContent = log
        ? `<pre class="core-log-text">${escapeHtml(log)}</pre>`
        : '<div class="core-log-placeholder">暂无核心输出，保持运行后有新输出会显示在这里。</div>';
      return `
        <article class="core-log-entry ${log ? "" : "is-empty"}">
          <header>
            <div>
              <strong>${escapeHtml(title)}</strong>
              <span>${escapeHtml(formatTime(entry.started_at))}</span>
            </div>
            <span class="status ${escapeHtml(status)}">${escapeHtml(status)}</span>
          </header>
          ${logContent}
        </article>
      `;
    })
    .join("");
}

async function showCoreLog(core) {
  if (!core) return;
  const modal = $("#coreLogModal");
  modal.hidden = false;
  document.body.classList.add("modal-open");
  modal.querySelector('[data-action="close-core-log"]')?.focus();
  $("#coreLogTitle").textContent = `${core} 运行日志`;
  $("#coreLogMeta").textContent = "加载中";
  $("#coreLogBody").innerHTML = '<div class="core-log-loading">正在读取日志...</div>';
  try {
    const payload = await api(`/api/core/logs?core=${encodeURIComponent(core)}`);
    renderCoreLogPayload(payload);
  } catch (error) {
    $("#coreLogMeta").textContent = "读取失败";
    $("#coreLogBody").innerHTML = `<div class="core-log-empty error"><strong>日志读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

async function copyText(text) {
  try {
    const result = await api("/api/clipboard", { text });
    if (!result.ok && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
    }
    showToast(`已复制 ${text}`);
  } catch (error) {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      showToast(`已复制 ${text}`);
      return;
    }
    showToast(error.message, "error");
  }
}

async function startNode(nodeId) {
  const bridge = await api("/api/bridges/start", { node_id: nodeId, core: "auto" });
  await loadStatus();
  await copyText(bridge.socks_url);
}

async function testNode(nodeId) {
  const result = await api("/api/nodes/test", { node_id: nodeId, core: "auto" });
  await loadStatus();
  if (result.ok) {
    showToast(`通信正常：${result.latency_ms} ms`);
  } else {
    showToast(result.message || "测试失败", "error");
  }
  return result;
}

async function launchBrowser(nodeId) {
  const result = await api("/api/browser/launch", { node_id: nodeId });
  await loadStatus();
  showToast(`已启动指纹浏览器：${result.region || ""} · ${result.proxy}`);
  return result;
}

// 停止某节点的指纹浏览器进程,并一并停止其本地代理。
async function stopBrowser(nodeId) {
  const result = await api("/api/browser/stop", { node_id: nodeId });
  await loadStatus();
  showToast("已停止浏览器和本地代理");
  return result;
}

// 一键下载并安装指纹内核。下载体积较大(约 150-250MB),提示用户耐心等待。
async function downloadKernel() {
  showToast("正在下载指纹内核，约 150-250MB，请耐心等待…");
  const result = await api("/api/browser/download-kernel", {});
  await loadStatus();
  if (result.already_installed) {
    showToast("指纹内核已安装");
  } else if (result.arch_warning) {
    // 架构不匹配(如 arm64 内核装到 Intel 机)时给出显著提示。
    showToast(result.arch_warning, "error");
  } else {
    showToast(`指纹内核安装完成：${result.version || ""}`);
  }
  return result;
}

async function handleAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  button.disabled = true;
  // 点击后立即进入加载态:隐藏图标、显示旋转 spinner,给用户即时反馈。
  // close-core-log / copy 这类瞬时操作不需要转圈。
  const spins = !["close-core-log", "copy"].includes(action);
  if (spins) button.classList.add("is-busy");
  try {
    if (action === "start") {
      await startNode(button.dataset.node);
    } else if (action === "test") {
      await testNode(button.dataset.node);
    } else if (action === "launch-browser") {
      await launchBrowser(button.dataset.node);
    } else if (action === "stop-browser") {
      await stopBrowser(button.dataset.node);
    } else if (action === "download-kernel") {
      await downloadKernel();
    } else if (action === "copy") {
      await copyText(button.dataset.text);
    } else if (action === "stop") {
      await api("/api/bridges/stop", { id: button.dataset.id });
      await loadStatus();
      showToast("已停止");
    } else if (action === "restart") {
      await api("/api/bridges/stop", { id: button.dataset.id });
      await api("/api/bridges/start", { node_id: button.dataset.node, core: "auto" });
      await loadStatus();
      showToast("已重启服务");
    } else if (action === "refresh-sub") {
      await api("/api/subscriptions/refresh", { id: button.dataset.id });
      await loadStatus();
      showToast("订阅已刷新");
    } else if (action === "delete-sub") {
      await api("/api/subscriptions/delete", { id: button.dataset.id });
      await loadStatus();
      showToast("订阅已删除");
    } else if (action === "clear-logs") {
      await api("/api/logs/clear", {});
      await loadStatus();
      showToast("连接日志已清空");
    } else if (action === "clear-system-logs") {
      await api("/api/system/logs/clear", {});
      await loadStatus();
      showToast("系统日志已清空");
    } else if (action === "reset-log-filters") {
      appState.logFilters.search = "";
      appState.logFilters.node = "all";
      appState.logFilters.status = "all";
      renderLogFilters();
      renderRequestLogs();
      showToast("日志筛选已重置");
    } else if (action === "show-core-log") {
      lastCoreLogTrigger = button;
      await showCoreLog(button.dataset.core);
    } else if (action === "close-core-log") {
      closeCoreLogModal();
    }
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.classList.remove("is-busy");
  }
}

async function handleImport(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  const formData = new FormData(form);
  submit.disabled = true;
  try {
    const result = await api("/api/subscriptions/import", {
      name: formData.get("name"),
      url: formData.get("url"),
      raw: formData.get("raw"),
    });
    form.reset();
    await loadStatus();
    showToast(`导入 ${result.subscription.node_count} 个节点`);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

function bindEvents() {
  // 侧边栏导航:点击切换视图。
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => setActiveView(item.dataset.view));
  });
  $("#importForm").addEventListener("submit", handleImport);
  document.addEventListener("click", handleAction);
  $("#refreshStatus").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.classList.add("is-busy");
    button.disabled = true;
    try {
      await loadStatus();
      showToast("状态已刷新");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      button.classList.remove("is-busy");
      button.disabled = false;
    }
  });
  $("#refreshList").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.classList.add("is-busy");
    button.disabled = true;
    try {
      await loadStatus();
      showToast("列表已刷新");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      button.classList.remove("is-busy");
      button.disabled = false;
    }
  });
  $("#stopAll").addEventListener("click", async () => {
    try {
      await api("/api/bridges/stop-all", {});
      await loadStatus();
      showToast("已停止全部端口");
    } catch (error) {
      showToast(error.message, "error");
    }
  });
  $("#testVisible").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const nodes = filteredNodes();
    if (!nodes.length) {
      showToast("没有可测试的节点", "error");
      return;
    }
    button.disabled = true;
    try {
      for (const node of nodes) {
        button.querySelector("span").textContent = `测试中 ${nodes.indexOf(node) + 1}/${nodes.length}`;
        await testNode(node.id);
      }
      showToast("可见节点测试完成");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      button.querySelector("span").textContent = "测试可见节点";
      button.disabled = false;
    }
  });
  $("#protocolFilter").addEventListener("change", (event) => {
    appState.filters.protocol = event.target.value;
    renderNodes();
  });
  $("#regionFilter").addEventListener("change", (event) => {
    appState.filters.region = event.target.value;
    renderNodes();
  });
  $("#searchInput").addEventListener("input", (event) => {
    appState.filters.search = event.target.value;
    renderNodes();
  });
  $("#logSearchInput").addEventListener("input", (event) => {
    appState.logFilters.search = event.target.value;
    renderRequestLogs();
  });
  $("#logNodeFilter").addEventListener("change", (event) => {
    appState.logFilters.node = event.target.value;
    renderRequestLogs();
  });
  $("#logStatusFilter").addEventListener("change", (event) => {
    appState.logFilters.status = event.target.value;
    renderRequestLogs();
  });
  $("#systemSearchInput").addEventListener("input", (event) => {
    appState.systemLogFilters.search = event.target.value;
    renderSystemLogs();
  });
  $("#systemLevelFilter").addEventListener("change", (event) => {
    appState.systemLogFilters.level = event.target.value;
    renderSystemLogs();
  });
  $("#coreLogModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeCoreLogModal();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#coreLogModal").hidden) {
      closeCoreLogModal();
    }
  });
}

bindEvents();
// 初始视图:优先使用上次记忆的选择(在 <html> 上的 data-initial-view)。
setActiveView(document.documentElement.getAttribute("data-initial-view") || "nodes");
loadStatus().catch((error) => showToast(error.message, "error"));
window.setInterval(() => loadStatus().catch(() => {}), 3500);
