const appState = {
  version: "",
  cores: {},
  features: { browser: true, browser_profile_sync: true, remote_browser: false, docker: false },
  runtime: { mode: "headless", docker: false },
  remoteBrowser: { configured: false, available: false, active_sessions: 0, error: "" },
  platform: null,
  fingerprint_chromium: null,
  active_browsers: [],
  browser_instances: [],
  subscriptions: [],
  nodes: [],
  bridges: [],
  tests: {},
  speedTest: { settings: null, tasks: [] },
  subscriptionSettings: { allow_non_public_urls: false },
  subscriptionRefresh: { tasks: [] },
  smartGateway: null,
  requestLogs: [],
  systemLogs: [],
  logCenter: { settings: null, storage: null, request_total: 0, system_total: 0 },
  requestLogPage: { page: 1, page_count: 1, total: 0, filtered_total: 0, limit: 50, loaded: false },
  systemLogPage: { page: 1, page_count: 1, total: 0, filtered_total: 0, limit: 50, loaded: false },
  pool: null,
  network: null,
  networkStatus: "loading",
  activeView: "nodes",
  filters: {
    protocol: "all",
    region: "all",
    subscription: "all",
    sort: "name",
    search: "",
  },
  // groupId -> true 表示折叠；缺省为展开。
  collapsedNodeGroups: loadCollapsedNodeGroups(),
  logFilters: {
    search: "",
    node: "all",
    status: "all",
  },
  systemLogFilters: {
    search: "",
    level: "all",
  },
  browserRecordNodeId: null,
  sync: null,
  syncOpenssl: null,
  diagnostics: { result: null, webrtc: null, running: false },
  // 批量选择功能：存储选中的节点 ID
  selectedNodeIds: new Set(),
};

const managementAuth = window.ProxyBridgeAuth;
const browserUi = window.ProxyBridgeBrowserUi;
const coreUploadClient = window.ProxyBridgeCoreUpload;
let managementToken = managementAuth.loadManagementToken(window);

const $ = (selector) => document.querySelector(selector);
let lastCoreLogTrigger = null;
let lastCoreUploadTrigger = null;
let lastBrowserRecordTrigger = null;
let lastRemoteBrowserLaunchTrigger = null;
let lastRemoteBrowserCredentialsTrigger = null;
let lastCoreLogTriggerKey = null;
let lastCoreUploadTriggerKey = null;
let lastBrowserRecordTriggerKey = null;
let lastRemoteBrowserLaunchTriggerKey = null;
let lastRemoteBrowserLaunchTriggerAction = null;
let lastRemoteBrowserCredentialsTriggerKey = null;
let lastRemoteBrowserCredentialsTriggerAction = null;
let pendingRemoteBrowserLaunch = null;
let remoteBrowserLaunchPromise = null;
let viewedRemoteBrowserInstanceId = null;
let remoteBrowserCredentialsRequestGeneration = 0;
let pendingModalFocusKey = null;
let coreLogRequestGeneration = 0;
let coreLogAbortController = null;
let kernelDownloadPromise = null;
let coreUploadPromise = null;
let statusRequestPromise = null;
let statusAbortController = null;
let statusPollTimer = null;
let syncOperationPromise = null;
let networkSettingsSavePromise = null;
let logPageRequestGeneration = 0;
let logFilterTimer = null;
let logSettingsHydrated = false;
let logSettingsDirty = false;
const actionInFlight = new Set();
const renderSignatures = new Map();

const STATUS_REQUEST_TIMEOUT_MS = 12_000;
const STATUS_POLL_VISIBLE_MS = 3_500;
const STATUS_POLL_HIDDEN_MS = 30_000;

// 侧边栏可切换的视图列表,顺序与 HTML 中 .nav-item / .view 一致。
const VIEW_IDS = ["nodes", "subscriptions", "bridges", "logs", "system", "settings", "sync", "diagnostics"];

function browserEnabled() {
  return appState.features?.browser !== false;
}

function remoteBrowserEnabled() {
  return appState.features?.remote_browser === true;
}

function dockerRuntime() {
  return appState.runtime?.docker === true || appState.runtime?.mode === "docker";
}

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
  renderActiveView({ force: true });
  applyActionInFlightState();
  if (view === "logs") {
    loadLogPage("request").catch((error) => showToast(error.message, "error"));
  } else if (view === "system") {
    loadLogPage("system").catch((error) => showToast(error.message, "error"));
  }
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
  setNavCount("navCountLogs", Number(appState.logCenter?.request_total ?? appState.requestLogs.length));
  setNavCount("navCountSystem", Number(appState.logCenter?.system_total ?? appState.systemLogs.length));
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
    list: '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/>',
    chevron: '<path d="M9 6l6 6-6 6"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.play}</svg>`;
}

function loadCollapsedNodeGroups() {
  try {
    const raw = localStorage.getItem("pb-node-group-collapsed");
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (e) {
    return {};
  }
}

function saveCollapsedNodeGroups() {
  try {
    localStorage.setItem("pb-node-group-collapsed", JSON.stringify(appState.collapsedNodeGroups || {}));
  } catch (e) {
    /* ignore quota / private mode */
  }
}

function isNodeGroupCollapsed(groupId) {
  return Boolean(appState.collapsedNodeGroups && appState.collapsedNodeGroups[groupId]);
}

function setNodeGroupCollapsed(groupId, collapsed) {
  if (!appState.collapsedNodeGroups) appState.collapsedNodeGroups = {};
  if (collapsed) appState.collapsedNodeGroups[groupId] = true;
  else delete appState.collapsedNodeGroups[groupId];
  saveCollapsedNodeGroups();
}

/** node_id -> subscription record (first match wins). */
function subscriptionIndexByNodeId() {
  const map = new Map();
  for (const sub of appState.subscriptions || []) {
    for (const nodeId of sub.node_ids || []) {
      if (nodeId && !map.has(nodeId)) map.set(nodeId, sub);
    }
  }
  return map;
}

async function api(path, body = null, { signal } = {}) {
  const headers = {};
  if (managementToken) headers[managementAuth.headerName] = managementToken;
  const options = { headers };
  if (body) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (signal) options.signal = signal;
  const response = await fetch(path, options);
  const responseToken = response.headers.get(managementAuth.headerName) || "";
  if (!managementAuth.validateResponseToken(window, managementToken, responseToken)) {
    managementToken = "";
    throw new Error("管理 API 凭证已失效，请从启动入口重新打开控制台");
  }
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function logQueryParams(kind, { includePage = true, format = "" } = {}) {
  const params = new URLSearchParams({ kind });
  if (includePage) {
    const pageState = kind === "request" ? appState.requestLogPage : appState.systemLogPage;
    params.set("page", String(pageState.page || 1));
    params.set("limit", String(pageState.limit || 50));
  }
  if (kind === "request") {
    const filters = appState.logFilters;
    if (filters.search.trim()) params.set("search", filters.search.trim());
    if (filters.node !== "all") params.set("node", filters.node);
    if (filters.status !== "all") params.set("status", filters.status);
  } else {
    const filters = appState.systemLogFilters;
    if (filters.search.trim()) params.set("search", filters.search.trim());
    if (filters.level !== "all") params.set("level", filters.level);
  }
  if (format) params.set("format", format);
  return params;
}

async function loadLogPage(kind) {
  const generation = ++logPageRequestGeneration;
  const payload = await api(`/api/logs?${logQueryParams(kind)}`);
  if (generation !== logPageRequestGeneration) return;
  const pageState = {
    page: Number(payload.page || 1),
    page_count: Number(payload.page_count || 1),
    total: Number(payload.total || 0),
    filtered_total: Number(payload.filtered_total || 0),
    limit: Number(payload.limit || 50),
    loaded: true,
  };
  if (kind === "request") {
    appState.requestLogs = payload.items || [];
    appState.requestLogPage = pageState;
    renderLogFilters();
    renderRequestLogs();
  } else {
    appState.systemLogs = payload.items || [];
    appState.systemLogPage = pageState;
    renderSystemLogs();
  }
}

function scheduleLogPageLoad(kind) {
  window.clearTimeout(logFilterTimer);
  logFilterTimer = window.setTimeout(() => {
    const pageState = kind === "request" ? appState.requestLogPage : appState.systemLogPage;
    pageState.page = 1;
    loadLogPage(kind).catch((error) => showToast(error.message, "error"));
  }, 240);
}

async function downloadLogExport(kind, format) {
  const headers = {};
  if (managementToken) headers[managementAuth.headerName] = managementToken;
  const response = await fetch(`/api/logs/export?${logQueryParams(kind, { includePage: false, format })}`, { headers });
  const responseToken = response.headers.get(managementAuth.headerName) || "";
  if (!managementAuth.validateResponseToken(window, managementToken, responseToken)) {
    managementToken = "";
    throw new Error("管理 API 凭证已失效，请从启动入口重新打开控制台");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([A-Za-z0-9._-]+)"/);
  const filename = match?.[1] || `proxybridge-${kind}-logs.${format}`;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
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

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (value < 60) return `${Math.round(value)} 秒`;
  if (value < 3600) return `${Math.round(value / 60)} 分钟`;
  if (value < 86400) return `${Math.round(value / 3600)} 小时`;
  return `${Math.round(value / 86400)} 天`;
}

function diagnosticStatusLabel(status) {
  return { ok: "正常", warn: "警告", error: "错误", skipped: "跳过" }[status] || "未知";
}

function diagnosticDetailsText(details) {
  if (!details || typeof details !== "object" || !Object.keys(details).length) return "";
  return JSON.stringify(details, null, 2);
}

function renderDiagnosticBridgeOptions() {
  const select = $("#diagnosticsBridgeSelect");
  if (!select) return;
  const previous = select.value;
  const bridges = (appState.bridges || []).filter(
    (bridge) => ["running", "starting"].includes(bridge.status) && Number(bridge.port) > 0
  );
  select.innerHTML = [
    '<option value="">自动选择运行中的代理</option>',
    ...bridges.map((bridge) => (
      `<option value="${escapeHtml(bridge.id)}">${escapeHtml(bridge.node_name || "未命名节点")} · ${escapeHtml(bridge.core || "core")} · ${Number(bridge.port)}</option>`
    )),
  ].join("");
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  select.disabled = !$("#diagnosticsNetworkEnabled")?.checked || !bridges.length;
}

function renderDiagnostics() {
  renderDiagnosticBridgeOptions();
  const container = $("#diagnosticsResults");
  const statusLine = $("#diagnosticsStatusLine");
  if (!container || !statusLine) return;
  const backendResults = appState.diagnostics.result?.results || [];
  const webrtc = appState.diagnostics.webrtc
    ? [{
        id: "webrtc",
        label: "WebRTC 泄漏风险",
        category: "browser",
        latency_ms: appState.diagnostics.webrtc.latency_ms || 0,
        ...appState.diagnostics.webrtc,
      }]
    : [];
  const results = [...backendResults, ...webrtc];
  if (!results.length) {
    statusLine.textContent = appState.diagnostics.running ? "正在检查" : "尚未运行";
    container.innerHTML = `<div class="settings-empty">${appState.diagnostics.running ? "正在运行逐项检查…" : "运行诊断后在此显示逐项结果"}</div>`;
    return;
  }
  const counts = { ok: 0, warn: 0, error: 0, skipped: 0 };
  results.forEach((result) => { counts[result.status] = (counts[result.status] || 0) + 1; });
  statusLine.textContent = `正常 ${counts.ok} · 警告 ${counts.warn} · 错误 ${counts.error} · 跳过 ${counts.skipped}`;
  container.innerHTML = results.map((result) => {
    const detailText = diagnosticDetailsText(result.details);
    return `
      <article class="diagnostic-result is-${escapeHtml(result.status)}">
        <span class="diagnostic-status-mark" aria-hidden="true"></span>
        <div class="diagnostic-result-copy">
          <div class="diagnostic-result-head">
            <strong>${escapeHtml(result.label)}</strong>
            <span class="diagnostic-status-label">${diagnosticStatusLabel(result.status)}</span>
            <span class="diagnostic-latency">${Number(result.latency_ms || 0)} ms</span>
          </div>
          <p>${escapeHtml(result.message)}</p>
          ${detailText ? `<details class="diagnostic-details"><summary>查看详情</summary><pre>${escapeHtml(detailText)}</pre></details>` : ""}
        </div>
      </article>`;
  }).join("");
}

async function runWebrtcDiagnostic() {
  const started = performance.now();
  try {
    const result = await window.ProxyBridgeDiagnostics.collectWebrtcCandidates();
    return { ...result, latency_ms: Math.max(0, Math.round(performance.now() - started)) };
  } catch (error) {
    return {
      status: "error",
      message: `WebRTC 检查失败（${error?.name || "Error"}）`,
      details: {},
      latency_ms: Math.max(0, Math.round(performance.now() - started)),
    };
  }
}

async function runDiagnostics({ webrtcOnly = false } = {}) {
  if (appState.diagnostics.running) return;
  appState.diagnostics.running = true;
  if (!webrtcOnly) appState.diagnostics.result = null;
  appState.diagnostics.webrtc = null;
  renderDiagnostics();
  const runButton = $("#diagnosticsRunButton");
  const webrtcButton = $("#diagnosticsWebrtcButton");
  runButton.disabled = true;
  webrtcButton.disabled = true;
  runButton.setAttribute("aria-busy", "true");
  webrtcButton.setAttribute("aria-busy", "true");
  runButton.classList.add("is-busy");
  try {
    const webrtcPromise = runWebrtcDiagnostic();
    const backendPromise = webrtcOnly
      ? Promise.resolve(null)
      : api("/api/diagnostics/run", {
          network: Boolean($("#diagnosticsNetworkEnabled")?.checked),
          bridge_id: $("#diagnosticsBridgeSelect")?.value || "",
        });
    const [backendSettlement, webrtcSettlement] = await Promise.allSettled([backendPromise, webrtcPromise]);
    if (!webrtcOnly) {
      appState.diagnostics.result = backendSettlement.status === "fulfilled"
        ? backendSettlement.value
        : window.ProxyBridgeDiagnostics.failedBackendResult(backendSettlement.reason);
    }
    appState.diagnostics.webrtc = webrtcSettlement.status === "fulfilled"
      ? webrtcSettlement.value
      : {
          status: "error",
          message: `WebRTC 检查失败（${webrtcSettlement.reason?.name || "Error"}）`,
          details: {},
          latency_ms: 0,
        };
    if (!webrtcOnly && backendSettlement.status === "rejected") {
      showToast(backendSettlement.reason?.message || "诊断请求失败", "error");
    } else {
      showToast(webrtcOnly ? "WebRTC 检查完成" : "诊断完成");
    }
  } finally {
    appState.diagnostics.running = false;
    runButton.disabled = false;
    webrtcButton.disabled = false;
    runButton.removeAttribute("aria-busy");
    webrtcButton.removeAttribute("aria-busy");
    runButton.classList.remove("is-busy");
    renderDiagnostics();
  }
}

// 通用网络设置表单：与 WebDAV 表单一样，状态轮询不能覆盖尚未保存的开关。
let networkFormHydrated = false;
let networkFormDirty = false;
let speedTestFormHydrated = false;
let speedTestFormDirty = false;

function markNetworkFormDirty() {
  networkFormDirty = true;
}

function applyNetworkSettingsToForm(network, { force = false } = {}) {
  if (networkFormDirty && !force) return;
  const allowLan = $("#allowLan");
  if (!allowLan) return;
  allowLan.checked = Boolean(network?.allow_lan);
  const security = network?.security || {};
  $("#lanAuthMode").value = security.auth_mode || "none";
  $("#lanUsername").value = security.username || "";
  $("#lanPassword").value = "";
  $("#lanToken").value = "";
  $("#lanToken").placeholder = security.token_configured
    ? "已配置，留空保持不变"
    : "至少 16 个字符";
  $("#lanAllowedCidrs").value = (security.allowed_cidrs || []).join("\n");
  $("#lanMaxConnections").value = Number(security.max_connections || 128);
  syncLanAuthFields();
  networkFormHydrated = true;
}

function syncLanAuthFields() {
  const mode = $("#lanAuthMode")?.value || "none";
  const basic = mode === "basic";
  const token = mode === "token";
  $("#lanUsernameField").hidden = !basic;
  $("#lanPasswordField").hidden = !basic;
  $("#lanTokenField").hidden = !token;
}

function networkLanAddresses(network) {
  const values = [
    network?.primary_lan_address,
    ...(Array.isArray(network?.lan_addresses) ? network.lan_addresses : []),
  ]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  return [...new Set(values)];
}

function networkUrlList(...values) {
  return [...new Set(
    values
      .flatMap((value) => (Array.isArray(value) ? value : [value]))
      .map((value) => String(value || "").trim())
      .filter(Boolean)
  )];
}

function proxyPortFromUrls(entry) {
  if (entry?.port != null && String(entry.port).trim()) return String(entry.port);
  for (const value of networkUrlList(entry?.socks_urls, entry?.socks_url, entry?.http_urls, entry?.http_url)) {
    if (!value) continue;
    try {
      const port = new URL(value).port;
      if (port) return port;
    } catch (_) {
      // Ignore malformed optional display URLs; the other protocol may still be usable.
    }
  }
  return "";
}

function normalizeNetworkProxyEntry(entry, fallback = {}) {
  if (!entry || typeof entry !== "object") return null;
  const socksUrls = networkUrlList(
    entry.socks_urls,
    entry.lan_socks_urls,
    entry.socks_url,
    entry.lan_socks_url,
    fallback.socks_urls,
    fallback.socks_url
  );
  const httpUrls = networkUrlList(
    entry.http_urls,
    entry.lan_http_urls,
    entry.http_url,
    entry.lan_http_url,
    fallback.http_urls,
    fallback.http_url
  );
  const normalized = {
    bridge_id: entry.bridge_id || entry.id || fallback.bridge_id || "",
    port: entry.port ?? fallback.port ?? "",
    node_name: entry.node_name || entry.name || fallback.node_name || "",
    socks_urls: socksUrls,
    http_urls: httpUrls,
  };
  normalized.port = proxyPortFromUrls(normalized);
  return normalized;
}

function networkProxyEntries(network) {
  const direct = (Array.isArray(network?.proxy_urls) ? network.proxy_urls : [])
    .map((entry) => normalizeNetworkProxyEntry(entry))
    .filter(Boolean);

  const entries = direct.length
    ? direct
    : runningBridges()
        .map((bridge) => {
          const lanUrls = bridge.lan_urls && typeof bridge.lan_urls === "object" ? bridge.lan_urls : {};
          return normalizeNetworkProxyEntry(
            {
              port: bridge.port,
              bridge_id: bridge.id,
              node_name: bridge.node_name,
              socks_urls: bridge.lan_socks_urls || lanUrls.socks_urls,
              socks_url: bridge.lan_socks_url || bridge.lan_socks5_url || lanUrls.socks_url,
              http_urls: bridge.lan_http_urls || lanUrls.http_urls,
              http_url: bridge.lan_http_url || lanUrls.http_url,
            },
            { bridge_id: bridge.id, port: bridge.port, node_name: bridge.node_name }
          );
        })
        .filter(Boolean);

  const seen = new Set();
  return entries.filter((entry) => {
    const key = [
      entry.bridge_id,
      entry.node_name,
      entry.port,
      ...entry.socks_urls,
      ...entry.http_urls,
    ].join("\n");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function proxyAddressFromUrl(url) {
  try {
    return new URL(url).hostname;
  } catch (_) {
    return "";
  }
}

function networkProxyUrlRow(label, url, entry) {
  if (!url) return "";
  const nodeName = entry.node_name || "本地代理";
  const port = entry.port || proxyPortFromUrls({ socks_urls: [url] }) || "未知";
  const address = proxyAddressFromUrl(url) || "未知";
  const copyLabel = `复制 ${nodeName} 的 ${label} 代理，地址 ${address}，端口 ${port}`;
  return `
    <div class="network-proxy-url-row">
      <div>
        <span>${escapeHtml(label)}</span>
        <code title="${escapeHtml(url)}">${escapeHtml(url)}</code>
      </div>
      <button class="row-button copy-icon" type="button" data-action="copy" data-text="${escapeHtml(url)}" aria-label="${escapeHtml(copyLabel)}" title="${escapeHtml(copyLabel)}">${icon("copy")}</button>
    </div>`;
}

function renderNetworkProxyList(network, networkStatus) {
  const list = $("#networkProxyList");
  const count = $("#networkProxyCount");
  if (!list || !count) return;

  if (!network) {
    count.textContent = "—";
    list.innerHTML = `<div class="settings-empty">${
      networkStatus === "loading" ? "正在加载网络状态…" : "网络状态暂时不可用，无法显示共享地址。"
    }</div>`;
    return;
  }

  const staleNotice = networkStatus === "error"
    ? `<div class="network-stale-note">状态刷新失败，以下为上次成功获取的结果。</div>`
    : "";

  const dockerManaged = network?.docker_managed === true;
  if (!network?.allow_lan) {
    count.textContent = "0 个";
    list.innerHTML = `${staleNotice}<div class="settings-empty">局域网共享已关闭，代理仅可在本机使用。</div>`;
    return;
  }

  const entries = networkProxyEntries(network);
  count.textContent = `${entries.length} 个`;
  if (!entries.length) {
    list.innerHTML = `${staleNotice}<div class="settings-empty">暂无运行中的本地代理。启动节点后，这里会显示${dockerManaged ? "已映射到宿主机" : "可供局域网设备使用"}的地址。</div>`;
    return;
  }

  const hasShareableUrl = entries.some((entry) => entry.socks_urls.length || entry.http_urls.length);
  if (!hasShareableUrl) {
    const reason = networkLanAddresses(network).length
      ? "后端尚未生成可用的共享地址"
      : dockerManaged ? "Docker 端口范围尚未正确发布" : "未检测到可用的局域网 IP";
    list.innerHTML = `${staleNotice}<div class="settings-empty">检测到 ${entries.length} 个运行中的本地代理，但${reason}，暂时无法供局域网设备连接。</div>`;
    return;
  }

  list.innerHTML = staleNotice + entries
    .map((entry) => {
      const title = entry.node_name || "本地代理";
      const port = entry.port ? `<span>端口 ${escapeHtml(entry.port)}</span>` : "";
      const urlRows = [
        ...entry.socks_urls.map((url) => networkProxyUrlRow("SOCKS5", url, entry)),
        ...entry.http_urls.map((url) => networkProxyUrlRow("HTTP / HTTPS", url, entry)),
      ].join("");
      return `
        <article class="network-proxy-entry">
          <header>
            <strong>${escapeHtml(title)}</strong>
            ${port}
          </header>
          ${urlRows || `<div class="settings-empty">此代理暂无可用的共享地址。</div>`}
        </article>`;
    })
    .join("");
}

function renderNetworkClients(network) {
  const list = $("#networkClientList");
  const count = $("#networkClientCount");
  if (!list || !count) return;
  const clients = Array.isArray(network?.clients) ? network.clients : [];
  count.textContent = `${clients.length} 个连接`;
  if (!clients.length) {
    list.innerHTML = `<div class="settings-empty">暂无活动连接</div>`;
    return;
  }
  list.innerHTML = clients.map((client) => {
    const endpoint = `${client.address || "未知"}:${client.port || "-"}`;
    return `
      <div class="network-client-row">
        <div>
          <strong>${escapeHtml(client.node_name || "本地代理")}</strong>
          <span>${escapeHtml(endpoint)} · ${escapeHtml(String(client.protocol || "detecting").toUpperCase())} · ${escapeHtml(formatDuration(Math.max(0, Math.floor(Date.now() / 1000) - Number(client.connected_at || 0))))}</span>
        </div>
        <button class="row-button danger" type="button" data-action="kick-client" data-id="${escapeHtml(client.id || "")}">踢出</button>
      </div>`;
  }).join("");
}

function networkSettingsRenderSnapshot() {
  const network = appState.network;
  return {
    status: appState.networkStatus,
    allow_lan: network?.allow_lan ?? null,
    listen_host: network?.listen_host || "",
    configurable: network?.configurable ?? true,
    docker_managed: network?.docker_managed === true,
    security: network?.security || null,
    clients: network?.clients || [],
    lan_addresses: networkLanAddresses(network),
    speed_test: appState.speedTest?.settings || null,
    logs: appState.logCenter || null,
    proxies: networkProxyEntries(network).map((entry) => ({
      bridge_id: entry.bridge_id,
      node_name: entry.node_name,
      port: entry.port,
      socks_urls: entry.socks_urls,
      http_urls: entry.http_urls,
    })),
  };
}

function syncNetworkSettingsControlState() {
  const unavailable = !networkFormHydrated || !appState.network;
  const disabled = unavailable || Boolean(networkSettingsSavePromise);
  const controls = [
    $("#allowLan"),
    $("#lanAuthMode"),
    $("#lanUsername"),
    $("#lanPassword"),
    $("#lanToken"),
    $("#lanAllowedCidrs"),
    $("#lanMaxConnections"),
    $("#networkSaveButton"),
  ].filter(Boolean);
  controls.forEach((control) => {
    control.disabled = disabled;
  });
  if (appState.network?.range_configurable === false && $("#allowLan")) {
    $("#allowLan").disabled = true;
  }
}

function renderNetworkSettings() {
  const network = appState.network;
  const networkStatus = appState.networkStatus;
  const hasSnapshot = Boolean(network);
  if (networkStatus === "ready" && hasSnapshot && (!networkFormHydrated || !networkFormDirty)) {
    applyNetworkSettingsToForm(network);
  }

  syncNetworkSettingsControlState();

  const isLoading = networkStatus === "loading" && !hasSnapshot;
  const isUnavailable = networkStatus === "error" && !hasSnapshot;
  const isStale = networkStatus === "error" && hasSnapshot;
  const enabled = Boolean(network?.allow_lan);
  const dockerManaged = network?.docker_managed === true;

  const settingsHint = $("#networkSettingsHint");
  if (settingsHint) {
    settingsHint.textContent = dockerManaged
      ? "Docker 模式下，容器内代理固定监听 0.0.0.0；宿主机可访问范围由 docker run / Compose 的端口映射决定。管理控制台仍建议仅映射到 127.0.0.1。"
      : "管理本地代理的访问范围。设置只影响代理端口，控制台始终仅允许从本机访问。";
  }
  const cardTitle = $("#networkSettingsCardTitle");
  if (cardTitle) cardTitle.textContent = dockerManaged ? "Docker 端口映射" : "局域网共享";
  const shareDescription = $("#networkShareDescription");
  if (shareDescription) {
    shareDescription.textContent = dockerManaged
      ? "容器内监听已启用且不可在控制台修改。请通过 Compose / docker run 决定哪些代理端口发布到宿主机。"
      : "默认关闭。开启后，运行中的 SOCKS5 和 HTTP / HTTPS 代理会接受私有 IPv4 网络连接。";
  }
  const shareWarning = $("#networkShareWarning p");
  if (shareWarning) {
    const authMode = network?.security?.auth_mode || "none";
    const authLabel = authMode === "basic" ? "用户名和密码" : authMode === "token" ? "Token" : "无认证";
    shareWarning.textContent = dockerManaged
      ? `请保持管理端口只发布到宿主机 127.0.0.1。代理端口当前使用${authLabel}，并受 IP/CIDR 白名单与连接数上限保护。`
      : `请仅在可信网络开启。代理端口当前使用${authLabel}，并受 IP/CIDR 白名单与连接数上限保护。Proxy Bridge 控制台不会因此对外开放。`;
  }

  const statusLine = $("#settingsStatusLine");
  if (statusLine) {
    if (isLoading) statusLine.textContent = "网络状态正在加载";
    else if (isUnavailable) statusLine.textContent = "网络状态暂时不可用";
    else statusLine.textContent = `${dockerManaged ? "Docker 端口映射托管" : enabled ? "局域网共享已开启" : "局域网共享已关闭"}${isStale ? "（状态可能已过期）" : ""}`;
  }

  const effectiveStatus = $("#networkEffectiveStatus");
  if (effectiveStatus) {
    if (isLoading) effectiveStatus.textContent = "正在加载";
    else if (isUnavailable) effectiveStatus.textContent = "不可用";
    else effectiveStatus.textContent = `${dockerManaged ? "容器监听已启用" : enabled ? "已共享" : "仅本机"}${isStale ? " · 状态过期" : ""}`;
    effectiveStatus.classList.toggle("is-enabled", enabled && !isStale);
    effectiveStatus.classList.toggle("is-stale", isStale || isUnavailable);
  }

  const listenHost = $("#networkListenHost");
  if (listenHost) {
    listenHost.textContent = hasSnapshot
      ? network.listen_host || (enabled ? "0.0.0.0" : "127.0.0.1")
      : "—";
  }

  const addressList = $("#networkAddressList");
  if (addressList) {
    const addresses = networkLanAddresses(network);
    addressList.innerHTML = addresses.length
      ? addresses.map((address) => `<code class="network-address-pill">${escapeHtml(address)}</code>`).join("")
      : `<span class="settings-empty-inline">${
          isLoading ? "正在检测…" : isUnavailable ? "暂时无法读取网络信息" : dockerManaged ? "由宿主机端口映射决定" : "未检测到可用的局域网 IP"
        }</span>`;
  }

  renderNetworkProxyList(network, networkStatus);
  renderNetworkClients(network);
  const speedSettings = appState.speedTest?.settings;
  if (speedSettings && (!speedTestFormHydrated || !speedTestFormDirty)) {
    $("#speedTestTargets").value = (speedSettings.targets || []).join("\n");
    $("#speedTestRotation").value = speedSettings.rotation || "round_robin";
    $("#speedTestStaleMinutes").value = Math.max(1, Math.round(Number(speedSettings.stale_after_seconds || 21600) / 60));
    $("#speedTestConcurrency").value = Number(speedSettings.batch_concurrency || 3);
    $("#speedTestRetries").value = Number(speedSettings.retry_failures || 0);
    speedTestFormHydrated = true;
  }
  const logSettings = appState.logCenter?.settings;
  if (logSettings && (!logSettingsHydrated || !logSettingsDirty)) {
    $("#logRetentionDays").value = Number(logSettings.retention_days || 14);
    $("#logMaxFileMiB").value = Math.round(Number(logSettings.max_file_bytes || 8388608) / 1048576);
    $("#logDiskQuotaMiB").value = Math.round(Number(logSettings.disk_quota_bytes || 134217728) / 1048576);
    logSettingsHydrated = true;
  }
  const storage = appState.logCenter?.storage;
  if ($("#logStorageUsage")) {
    $("#logStorageUsage").textContent = storage
      ? `${formatBytes(storage.used_bytes)} / ${formatBytes(storage.quota_bytes)} · ${Number(storage.file_count || 0)} 个文件`
      : "—";
  }
}

async function saveSpeedTestSettings(event) {
  event.preventDefault();
  const targets = $("#speedTestTargets").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  const payload = {
    targets,
    rotation: $("#speedTestRotation").value,
    stale_after_seconds: Number($("#speedTestStaleMinutes").value || 360) * 60,
    batch_concurrency: Number($("#speedTestConcurrency").value || 3),
    retry_failures: Number($("#speedTestRetries").value || 0),
  };
  const result = await api("/api/settings/speed-test", payload);
  appState.speedTest.settings = result.settings;
  speedTestFormDirty = false;
  renderNetworkSettings();
  showToast("测速设置已保存");
}

async function saveLogSettings(event) {
  event.preventDefault();
  const result = await api("/api/settings/logs", {
    retention_days: Number($("#logRetentionDays").value),
    max_file_bytes: Number($("#logMaxFileMiB").value) * 1048576,
    disk_quota_bytes: Number($("#logDiskQuotaMiB").value) * 1048576,
  });
  const saved = result.settings || {};
  appState.logCenter.settings = {
    retention_days: saved.retention_days,
    max_file_bytes: saved.max_file_bytes,
    disk_quota_bytes: saved.disk_quota_bytes,
  };
  if (saved.storage) appState.logCenter.storage = saved.storage;
  logSettingsDirty = false;
  renderNetworkSettings();
  showToast(`日志设置已保存，清理 ${Number(saved.cleanup?.deleted_files || 0)} 个文件`);
}

function setNetworkSettingsBusy(busy, trigger = null) {
  syncNetworkSettingsControlState();
  if (trigger) {
    trigger.classList.toggle("is-busy", busy);
    if (busy) trigger.setAttribute("aria-busy", "true");
    else trigger.removeAttribute("aria-busy");
  }
}

async function withNetworkSettingsSave(task, trigger = null) {
  if (networkSettingsSavePromise) {
    throw new Error("网络设置正在保存，请稍候");
  }
  const operation = Promise.resolve().then(task);
  networkSettingsSavePromise = operation;
  setNetworkSettingsBusy(true, trigger);
  try {
    return await operation;
  } finally {
    if (networkSettingsSavePromise === operation) networkSettingsSavePromise = null;
    setNetworkSettingsBusy(false, trigger);
  }
}

async function saveNetworkSettings(event) {
  event.preventDefault();
  const allowLan = appState.network?.range_configurable === false
    ? true
    : Boolean($("#allowLan")?.checked);
  const result = await api("/api/settings/network", {
    allow_lan: allowLan,
    lan_security: {
      auth_mode: $("#lanAuthMode").value,
      username: $("#lanUsername").value,
      password: $("#lanPassword").value,
      token: $("#lanToken").value,
      allowed_cidrs: $("#lanAllowedCidrs").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      max_connections: Number($("#lanMaxConnections").value),
    },
  });
  appState.network = result.network || { ...(appState.network || {}), allow_lan: allowLan };
  appState.networkStatus = "ready";
  networkFormDirty = false;
  applyNetworkSettingsToForm(appState.network, { force: true });
  renderNetworkSettings();

  try {
    await loadStatus({ forceFresh: true });
  } catch (error) {
    showToast(`设置已保存，但状态刷新失败：${error.message}`, "error");
    return result;
  }

  const reboundCount = Number(result.rebound_count || 0);
  showToast(
    reboundCount > 0
      ? `网络设置已保存，已重新绑定 ${reboundCount} 个运行中代理，原有连接可能已中断`
      : "网络设置已保存"
  );
  return result;
}

// WebDAV 表单：状态轮询会反复 render，不能把未保存的输入写回成服务端旧值。
let syncFormHydrated = false;
let syncFormDirty = false;

function markSyncFormDirty() {
  syncFormDirty = true;
}

function applySyncSettingsToForm(settings, { force = false } = {}) {
  if (!settings) return;
  if (syncFormDirty && !force) return;
  const url = $("#syncUrl");
  if (!url) return;
  url.value = settings.url || "";
  $("#syncUsername").value = settings.username || "";
  $("#syncPassword").value = "";
  $("#syncPassword").placeholder = settings.password_set ? "已保存，留空不改" : "WebDAV 密码";
  $("#syncRemoteRoot").value = settings.remote_root || "ProxyBridge";
  $("#syncEnabled").checked = Boolean(settings.enabled);
  $("#syncAutoUpload").checked = Boolean(
    settings.browser_sync_enabled !== false && settings.auto_upload_on_browser_stop
  );
  $("#syncCheckStartup").checked = Boolean(settings.check_on_startup);
  const insecure = $("#syncInsecureSsl");
  if (insecure) insecure.checked = Boolean(settings.insecure_ssl);
  $("#syncEncrypt").checked = Boolean(settings.encrypt);
  $("#syncEncryptPassword").value = "";
  $("#syncEncryptPassword").placeholder = settings.encrypt_password_set ? "已设置，留空不改" : "加密密码";
  syncFormHydrated = true;
}

function renderSyncMeta(settings) {
  const line = $("#syncStatusLine");
  if (line) {
    if (!settings.url) line.textContent = "未配置";
    else if (settings.enabled) line.textContent = settings.last_status || "已启用";
    else line.textContent = "已配置 · 未启用";
  }
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  setText("syncDeviceId", settings.device_id || "—");
  setText(
    "syncScope",
    settings.sync_scope === "subscriptions" ? "仅订阅 / 节点" : "订阅 / 节点 + 浏览器 Profile"
  );
  setText("syncLastPush", formatTime(settings.last_push_at));
  setText("syncLastPull", formatTime(settings.last_pull_at));
  setText("syncLastRemote", formatTime(settings.last_remote_updated_at));
  setText("syncLastStatus", settings.last_status || "—");
  setText("syncLastError", settings.last_error || "—");
  setText(
    "syncOpenssl",
    appState.syncOpenssl == null ? "—" : appState.syncOpenssl ? "可用" : "未找到（加密不可用）"
  );
}

function renderSync() {
  const settings = appState.sync || {};
  // 仅首次灌入或表单未改动时同步输入框；元数据始终刷新。
  if (!syncFormHydrated || !syncFormDirty) {
    applySyncSettingsToForm(settings);
  }
  const browserSyncEnabled = settings.browser_sync_enabled !== false && browserEnabled();
  const description = $("#syncDescription");
  if (description) {
    description.innerHTML = browserSyncEnabled
      ? "在多台设备之间同步<strong>订阅 / 节点</strong>与<strong>已停止的指纹浏览器 Profile</strong>（Cookie、LocalStorage 等），实现换机接力。删除订阅会清理关联浏览器数据；上传前请先关闭目标浏览器。"
      : "当前为 Docker / 无浏览器模式，WebDAV 仅同步<strong>订阅与节点</strong>。上传不会打包、覆盖或删除远端已有的浏览器 Profile；拉取也会保留本地浏览器数据。";
  }
  const autoUploadOption = $("#syncAutoUploadOption");
  if (autoUploadOption) autoUploadOption.hidden = !browserSyncEnabled;
  const autoUpload = $("#syncAutoUpload");
  if (autoUpload) autoUpload.disabled = !browserSyncEnabled;
  renderSyncMeta(settings);
}

async function saveSyncSettings(event) {
  event.preventDefault();
  const payload = {
    url: $("#syncUrl").value.trim(),
    username: $("#syncUsername").value.trim(),
    remote_root: $("#syncRemoteRoot").value.trim() || "ProxyBridge",
    enabled: $("#syncEnabled").checked,
    auto_upload_on_browser_stop: Boolean(
      appState.sync?.browser_sync_enabled !== false
      && browserEnabled()
      && $("#syncAutoUpload").checked
    ),
    check_on_startup: $("#syncCheckStartup").checked,
    insecure_ssl: Boolean($("#syncInsecureSsl")?.checked),
    encrypt: $("#syncEncrypt").checked,
  };
  const password = $("#syncPassword").value;
  if (password) payload.password = password;
  const enc = $("#syncEncryptPassword").value;
  if (enc) payload.encrypt_password = enc;
  const result = await api("/api/sync/settings", payload);
  appState.sync = result.settings || null;
  // 保存成功后以服务端结果回填，并清空密码输入框。
  syncFormDirty = false;
  applySyncSettingsToForm(result.settings || {}, { force: true });
  renderSyncMeta(result.settings || {});
  showToast("同步设置已保存");
}

async function runSyncAction(kind) {
  const paths = {
    test: "/api/sync/test",
    push: "/api/sync/push",
    pull: "/api/sync/pull",
    check: "/api/sync/check",
  };
  const path = paths[kind];
  if (!path) return;
  const result = await api(path, {});
  if (result.settings) appState.sync = result.settings;
  // 操作结果只刷新状态文案，不覆盖用户正在编辑的表单。
  if (kind === "pull") await loadStatus({ forceFresh: true });
  else renderSyncMeta(appState.sync || {});
  if (kind === "check") {
    if (!result.has_remote) showToast("远端尚无同步数据");
    else if (result.already_latest || !result.remote_newer) showToast("已是最新，无需拉取");
    else showToast("远端有更新，可点击「从远端拉取」");
  } else if (kind === "test") {
    showToast(
      result.layout_ok === false
        ? `连接成功，但创建目录失败：${result.layout_error || ""}`
        : "连接测试成功"
    );
  } else if (kind === "push") {
    const n = (result.uploaded_profiles || []).length;
    const skip = (result.skipped_unchanged || []).length;
    if (result.ok === false) showToast((result.errors || []).join("; ") || "上传有错误", "error");
    else if (result.settings?.sync_scope === "subscriptions") showToast("订阅与节点上传完成");
    else showToast(`上传完成：${n} 个 Profile，跳过未变 ${skip}`);
  } else if (kind === "pull") {
    if (result.skipped && result.reason === "already_latest") showToast("已是最新，跳过拉取");
    else if (result.conflict_warning) showToast(result.conflict_warning);
    else if (result.ok === false) showToast((result.errors || []).join("; ") || "拉取有错误", "error");
    else showToast("拉取完成");
  }
  return result;
}

function setSyncControlsBusy(busy, trigger = null) {
  const controls = [
    "syncCheckBtn",
    "syncTestBtn",
    "syncPushBtn",
    "syncPullBtn",
  ]
    .map((id) => document.getElementById(id))
    .filter(Boolean);
  const submit = $("#syncForm button[type=\"submit\"]");
  if (submit) controls.push(submit);
  controls.forEach((control) => {
    if (busy) {
      if (!("syncPreviousDisabled" in control.dataset)) {
        control.dataset.syncPreviousDisabled = control.disabled ? "true" : "false";
      }
      control.disabled = true;
    } else if ("syncPreviousDisabled" in control.dataset) {
      control.disabled = control.dataset.syncPreviousDisabled === "true";
      delete control.dataset.syncPreviousDisabled;
    }
    control.classList.toggle("is-busy", busy && control === trigger);
  });
}

async function withSyncOperation(task, trigger = null) {
  if (syncOperationPromise) {
    throw new Error("已有同步操作正在进行，请等待完成");
  }
  const operation = Promise.resolve().then(task);
  syncOperationPromise = operation;
  setSyncControlsBusy(true, trigger);
  try {
    return await operation;
  } finally {
    if (syncOperationPromise === operation) syncOperationPromise = null;
    setSyncControlsBusy(false, trigger);
  }
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

function nodeById(nodeId) {
  return appState.nodes.find((node) => node.id === nodeId);
}

function browserInstanceById(instanceId) {
  return (appState.browser_instances || []).find((instance) => instance.id === instanceId) || null;
}

function safeRemoteBrowserUrl(value) {
  return browserUi.safeRemoteBrowserUrl(value, URL);
}

function browserInstancesForNode(nodeId) {
  return browserUi.sortBrowserInstancesByRecentUse(
    (appState.browser_instances || []).filter((instance) => instance.node_id === nodeId)
  );
}

function runningBrowserInstancesForNode(nodeId) {
  return browserInstancesForNode(nodeId).filter((instance) => ["running", "starting", "stopping"].includes(instance.status));
}

function browserStatusLabel(status) {
  const labels = {
    running: "运行中",
    starting: "启动中",
    stopping: "停止中",
    stopped: "已停止",
    failed: "失败",
  };
  return labels[status] || status || "-";
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

function renderSignature(value) {
  try {
    return JSON.stringify(value);
  } catch (_) {
    return String(value);
  }
}

function renderWhenChanged(key, value, renderer, force = false) {
  const signature = renderSignature(value);
  if (!force && renderSignatures.get(key) === signature) return;
  renderSignatures.set(key, signature);
  renderer();
}

function renderActiveView({ force = false } = {}) {
  switch (appState.activeView) {
    case "nodes":
      renderWhenChanged(
        "nodes",
        {
          nodes: appState.nodes,
          subscriptions: appState.subscriptions,
          bridges: appState.bridges,
          tests: appState.tests,
          speedTest: appState.speedTest,
          browserEnabled: browserEnabled(),
          remoteBrowserEnabled: remoteBrowserEnabled(),
          remoteBrowser: appState.remoteBrowser,
          activeBrowsers: appState.active_browsers,
          browserInstances: appState.browser_instances,
          filters: appState.filters,
          collapsed: appState.collapsedNodeGroups,
        },
        () => {
          renderFilters();
          renderNodes();
        },
        force
      );
      break;
    case "subscriptions":
      renderWhenChanged(
        "subscriptions",
        {
          subscriptions: appState.subscriptions,
          settings: appState.subscriptionSettings,
          refresh: appState.subscriptionRefresh,
        },
        renderSubscriptions,
        force
      );
      break;
    case "bridges":
      renderWhenChanged(
        "bridges",
        {
          bridges: appState.bridges,
          pool: appState.pool,
          smartGateway: appState.smartGateway,
          docker: dockerRuntime(),
        },
        () => {
          renderPool();
          renderBridges();
        },
        force
      );
      break;
    case "logs":
      renderWhenChanged(
        "logs",
        { logs: appState.requestLogs, filters: appState.logFilters, nodes: appState.nodes },
        () => {
          renderLogFilters();
          renderRequestLogs();
        },
        force
      );
      break;
    case "system":
      renderWhenChanged(
        "system",
        { logs: appState.systemLogs, filters: appState.systemLogFilters },
        renderSystemLogs,
        force
      );
      break;
    case "settings":
      renderWhenChanged(
        "settings",
        networkSettingsRenderSnapshot(),
        renderNetworkSettings,
        force
      );
      break;
    case "sync":
      renderWhenChanged(
        "sync",
        { settings: appState.sync, openssl: appState.syncOpenssl, browserEnabled: browserEnabled() },
        renderSync,
        force
      );
      break;
    case "diagnostics":
      renderWhenChanged(
        "diagnostics",
        { diagnostics: appState.diagnostics, bridges: appState.bridges },
        renderDiagnostics,
        force
      );
      break;
    default:
      break;
  }
}

async function loadStatus({ forceFresh = false } = {}) {
  if (statusRequestPromise) {
    const current = statusRequestPromise;
    if (!forceFresh) return current;
    try {
      await current;
    } catch (_) {
      // A fresh request below provides the authoritative result.
    }
    return loadStatus();
  }

  const controller = new AbortController();
  statusAbortController = controller;
  const timeout = window.setTimeout(() => controller.abort(), STATUS_REQUEST_TIMEOUT_MS);
  const request = (async () => {
    try {
      const status = await api("/api/status", null, { signal: controller.signal });
      appState.version = status.version || "";
      appState.features = status.features || {
        browser: true,
        browser_profile_sync: true,
        remote_browser: false,
        docker: false,
      };
      appState.runtime = status.runtime || { mode: status.platform?.shell_mode || "headless", docker: false };
      appState.remoteBrowser = status.remote_browser || {
        configured: false,
        available: false,
        active_sessions: 0,
        error: "",
      };
      appState.platform = status.platform || null;
      appState.cores = status.cores || {};
      appState.fingerprint_chromium = status.fingerprint_chromium || null;
      appState.active_browsers = status.active_browsers || [];
      appState.browser_instances = status.browser_instances || [];
      appState.subscriptions = status.subscriptions || [];
      appState.nodes = status.nodes || [];
      appState.bridges = status.bridges || [];
      appState.tests = status.tests || {};
      appState.speedTest = status.speed_test || { settings: null, tasks: [] };
      appState.subscriptionSettings = status.subscription_settings || { allow_non_public_urls: false };
      appState.subscriptionRefresh = status.subscription_refresh || { tasks: [] };
      appState.smartGateway = status.smart_gateway || null;
      appState.logCenter = status.logs || appState.logCenter;
      if (appState.activeView !== "logs" || !appState.requestLogPage.loaded) {
        appState.requestLogs = status.request_logs || [];
      }
      if (appState.activeView !== "system" || !appState.systemLogPage.loaded) {
        appState.systemLogs = status.system_logs || [];
      }
      appState.pool = status.pool || null;
      if (status.network && typeof status.network === "object") {
        appState.network = status.network;
        appState.networkStatus = "ready";
      } else {
        appState.networkStatus = "error";
      }
      appState.sync = status.sync || appState.sync || null;
      if ("sync_openssl" in status) appState.syncOpenssl = Boolean(status.sync_openssl);
      render();
      return true;
    } catch (error) {
      let statusError = error;
      if (error?.name === "AbortError") {
        statusError = new Error("状态请求超时，请检查后端是否仍在运行");
      }
      appState.networkStatus = "error";
      renderActiveView();
      throw statusError;
    } finally {
      window.clearTimeout(timeout);
      if (statusAbortController === controller) statusAbortController = null;
    }
  })();
  statusRequestPromise = request;
  try {
    return await request;
  } finally {
    if (statusRequestPromise === request) statusRequestPromise = null;
  }
}

function render() {
  renderWhenChanged(
    "nav",
    {
      nodes: appState.nodes.length,
      subscriptions: appState.subscriptions.length,
      bridges: runningBridges().length,
      logs: Number(appState.logCenter?.request_total ?? appState.requestLogs.length),
      system: Number(appState.logCenter?.system_total ?? appState.systemLogs.length),
    },
    renderNav
  );
  renderWhenChanged(
    "cores",
    {
      cores: appState.cores,
      fingerprint: appState.fingerprint_chromium,
      downloadBusy: Boolean(kernelDownloadPromise),
      uploadBusy: Boolean(coreUploadPromise),
      browserEnabled: browserEnabled(),
    },
    renderCores
  );
  const brandSubtitle = $("#brandSubtitle");
  if (brandSubtitle) {
    const runtimeLabel = dockerRuntime() ? "Docker 控制台 · 订阅节点" : "订阅节点 · 本地代理";
    const versionLabel = String(appState.version || "").trim();
    brandSubtitle.textContent = versionLabel ? `v${versionLabel} · ${runtimeLabel}` : runtimeLabel;
  }
  const remoteLaunchModal = $("#remoteBrowserLaunchModal");
  const remoteLaunchHasCredentials = !$("#remoteBrowserLaunchSuccess").hidden;
  if (
    !remoteBrowserEnabled()
    && !remoteLaunchModal.hidden
    && !remoteBrowserLaunchPromise
    && !remoteLaunchHasCredentials
  ) {
    closeRemoteBrowserLaunchModal({ force: true });
  } else if (remoteBrowserEnabled() && !remoteLaunchModal.hidden) {
    renderRemoteBrowserBrokerStatus();
  }
  const credentialsModal = $("#remoteBrowserCredentialsModal");
  if (credentialsModal && !credentialsModal.hidden) {
    const viewedInstance = browserInstanceById(viewedRemoteBrowserInstanceId);
    if (!remoteBrowserEnabled() || !viewedInstance || viewedInstance.status !== "running") {
      closeRemoteBrowserCredentialsModal();
    }
  }
  if (!browserEnabled() && !$("#browserRecordModal").hidden) {
    closeBrowserRecordModal();
  }
  renderActiveView();
  if (appState.browserRecordNodeId && !$("#browserRecordModal").hidden) {
    // 正在输入标签时只刷新标题元信息，避免整表重建打断输入。
    const ae = document.activeElement;
    const typingLabel = ae && ae.classList && ae.classList.contains("browser-label-input");
    if (typingLabel) {
      const node = nodeById(appState.browserRecordNodeId);
      const instances = browserInstancesForNode(appState.browserRecordNodeId);
      const runningCount = instances.filter((i) => ["running", "starting", "stopping"].includes(i.status)).length;
      $("#browserRecordTitle").textContent = `${node?.name || "节点"} 浏览器记录`;
      $("#browserRecordMeta").textContent = `${instances.length} 个记录 · ${runningCount} 个运行中`;
    } else {
      renderBrowserRecords(appState.browserRecordNodeId);
    }
  }
  applyActionInFlightState();
}

function renderCores() {
  const coreStrip = $("#coreStrip");
  const singBox = appState.cores["sing-box"];
  const xray = appState.cores.xray;
  const fpKernel = appState.fingerprint_chromium;
  const installBusy = Boolean(kernelDownloadPromise || coreUploadPromise);
  const kernelBusyClass = installBusy ? " is-busy" : "";
  const kernelBusyAttributes = installBusy ? ' disabled aria-busy="true"' : "";
  const uploadBusyLabel = coreUploadPromise ? "正在导入" : "本地导入";
  // 指纹内核状态:已安装显示 ready,缺失显示可点击的「下载指纹内核」按钮。
  const fpPill = !browserEnabled()
    ? ""
    : fpKernel
      ? `<button class="pill core-pill ok${kernelBusyClass}" type="button" data-action="download-kernel" aria-label="指纹内核已就绪，点击可重新下载" title="指纹内核已就绪：${escapeHtml(fpKernel)}"${kernelBusyAttributes}><span>指纹内核 ready</span></button>`
      : `<button class="pill core-pill warn${kernelBusyClass}" type="button" data-action="download-kernel" aria-label="下载指纹内核 fingerprint-chromium" title="点击自动下载安装指纹内核 fingerprint-chromium"${kernelBusyAttributes}><span>下载指纹内核</span></button>`;
  coreStrip.innerHTML = `
    <button class="pill core-pill ${singBox ? "ok" : "warn"}" type="button" data-action="show-core-log" data-core="sing-box" aria-label="查看 sing-box 运行日志" title="查看 sing-box 运行日志"><span>sing-box ${singBox ? "ready" : "missing"}</span></button>
    <button class="pill core-pill ${xray ? "ok" : "warn"}" type="button" data-action="show-core-log" data-core="xray" aria-label="查看 xray 运行日志" title="查看 xray 运行日志"><span>xray ${xray ? "ready" : "missing"}</span></button>
    ${fpPill}
    <button class="pill core-pill${coreUploadPromise ? " is-busy" : ""}" type="button" data-action="show-core-upload" aria-label="从本地文件导入代理或指纹内核" title="上传官方 Release 安装包，绕过 GitHub 在线下载"${installBusy ? ' disabled aria-busy="true"' : ""}><span>${uploadBusyLabel}</span></button>
  `;
}

function renderSubscriptions() {
  const list = $("#subscriptionList");
  const count = $("#subsCount");
  if (count) count.textContent = `${appState.subscriptions.length} 个订阅`;
  const allowNonPublic = $("#allowNonPublicSubscriptions");
  if (allowNonPublic) {
    allowNonPublic.checked = Boolean(appState.subscriptionSettings?.allow_non_public_urls);
  }
  renderSubscriptionRefreshTask();
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
            <div class="subscription-copy">
              <div class="subscription-title-row">
                <strong class="subscription-name">${escapeHtml(item.name)}</strong>
                <span class="sub-status ${item.last_error ? "is-error" : ""}">${item.last_error ? "需检查" : "正常"}</span>
              </div>
              <div class="subscription-meta">${item.node_count || 0} 个节点 · 更新于 ${formatTime(item.last_update)}</div>
              ${item.url ? `<div class="subscription-url" title="${escapeHtml(item.url)}">${escapeHtml(item.url)}</div>` : '<div class="subscription-url">手动导入</div>'}
              ${item.last_error ? `<div class="subscription-error" title="${escapeHtml(item.last_error)}">${escapeHtml(item.last_error)}</div>` : ""}
            </div>
          </div>
          <div class="item-actions">
            <label class="field-inline">
              <span>自动刷新</span>
              <select class="subscription-schedule" data-action="schedule-sub" data-id="${escapeHtml(item.id)}">
                ${[
                  [0, "关闭"],
                  [15, "每 15 分钟"],
                  [30, "每 30 分钟"],
                  [60, "每小时"],
                  [360, "每 6 小时"],
                  [1440, "每天"],
                ].map(([value, label]) => `<option value="${value}" ${Number(item.refresh_interval_minutes || 0) === value ? "selected" : ""}>${label}</option>`).join("")}
              </select>
            </label>
            ${
              item.url
                ? `<button class="row-button" data-action="refresh-sub" data-id="${escapeHtml(item.id)}">${icon("refresh")}<span>刷新</span></button>`
                : ""
            }
            <button class="row-button danger" data-action="delete-sub" data-id="${escapeHtml(item.id)}">${icon("trash")}<span>删除</span></button>
          </div>
          ${renderSubscriptionHistory(item)}
        </article>
      `
    )
    .join("");
}

function renderSubscriptionHistory(item) {
  const history = Array.isArray(item.refresh_history) ? item.refresh_history.slice(-3).reverse() : [];
  if (!history.length) return "";
  const labels = { updated: "已更新", not_modified: "无变化", failed: "失败", cancelled: "已取消" };
  return `<details class="subscription-history"><summary>最近刷新记录 · ${history.length}</summary><div>${history.map((entry) => {
    const changes = entry.changes || {};
    const detail = entry.status === "updated"
      ? `+${Number(changes.added || 0)} / -${Number(changes.removed || 0)} / ~${Number(changes.changed || 0)}`
      : entry.error ? escapeHtml(entry.error) : `${Number(entry.duration_ms || 0)} ms`;
    return `<span class="history-${escapeHtml(entry.status)}">${escapeHtml(labels[entry.status] || entry.status)} · ${formatTime(entry.at)} · ${detail}</span>`;
  }).join("")}</div></details>`;
}

function activeSubscriptionRefreshTask() {
  const tasks = Array.isArray(appState.subscriptionRefresh?.tasks) ? appState.subscriptionRefresh.tasks : [];
  return [...tasks].reverse().find((task) => ["queued", "running", "cancelling"].includes(task.status)) || null;
}

function subscriptionRefreshItemDetail(item) {
  if (!item) return "等待执行";
  if (item.status === "updated") {
    const changes = item.changes || {};
    return `+${Number(changes.added || 0)} / -${Number(changes.removed || 0)} / ~${Number(changes.changed || 0)}`;
  }
  if (item.error) return item.error;
  return `${Number(item.duration_ms || 0)} ms`;
}

function renderSubscriptionRefreshTask() {
  const container = $("#subscriptionRefreshTask");
  const button = $("#refreshAllSubscriptions");
  if (!container || !button) return;
  const tasks = Array.isArray(appState.subscriptionRefresh?.tasks) ? appState.subscriptionRefresh.tasks : [];
  const activeTask = activeSubscriptionRefreshTask();
  const task = activeTask || tasks[tasks.length - 1] || null;
  if (!task) {
    container.hidden = true;
    button.disabled = false;
    button.querySelector("span").textContent = "刷新全部";
    return;
  }
  container.hidden = false;
  const completed = Number(task.completed || 0);
  const total = Number(task.total || 0);
  const taskLabels = {
    queued: "等待开始",
    running: "进行中",
    cancelling: "正在取消",
    completed: "已完成",
    cancelled: "已取消",
  };
  const itemLabels = {
    updated: "已更新",
    not_modified: "无变化",
    failed: "失败",
    cancelled: "已取消",
    running: "刷新中",
    queued: "等待",
  };
  const subscriptionNames = new Map(appState.subscriptions.map((item) => [item.id, item.name]));
  const subscriptionIds = Array.isArray(task.subscription_ids) ? task.subscription_ids : [];
  const items = Array.isArray(task.items) ? task.items : [];
  const itemRows = subscriptionIds.map((subscriptionId, index) => {
    const item = items[index];
    const pendingStatus = task.active_index === index ? "running" : "queued";
    const status = item?.status || pendingStatus;
    const statusClass = Object.prototype.hasOwnProperty.call(itemLabels, status) ? status : "queued";
    const name = subscriptionNames.get(subscriptionId) || `订阅 ${index + 1}`;
    return `
      <div class="subscription-task-item is-${statusClass}">
        <strong>${escapeHtml(name)}</strong>
        <span class="subscription-task-result">${escapeHtml(itemLabels[status] || status)} · ${escapeHtml(subscriptionRefreshItemDetail(item))}</span>
      </div>`;
  }).join("");
  const cancelButton = activeTask
    ? `<button type="button" class="row-button" data-action="cancel-refresh-all" data-id="${escapeHtml(task.id)}"${task.status === "cancelling" ? " disabled" : ""}>${task.status === "cancelling" ? "正在取消" : "取消后续刷新"}</button>`
    : "";
  container.innerHTML = `
    <div class="subscription-task-head">
      <span>批量刷新 ${completed}/${total} · ${escapeHtml(taskLabels[task.status] || task.status)}</span>
      ${cancelButton}
    </div>
    <div class="subscription-task-items">${itemRows}</div>`;
  button.disabled = Boolean(activeTask);
  button.querySelector("span").textContent = activeTask ? `刷新中 ${completed}/${total}` : "再次刷新全部";
}

function setSelectOptions(select, values, current, allLabel) {
  const normalized = ["all", ...values.filter(Boolean).sort((a, b) => a.localeCompare(b, "zh-CN"))];
  select.innerHTML = normalized
    .map((value) => `<option value="${escapeHtml(value)}">${value === "all" ? allLabel : escapeHtml(value)}</option>`)
    .join("");
  select.value = normalized.includes(current) ? current : "all";
  return select.value;
}

function renderFilters() {
  const protocols = [...new Set(appState.nodes.map((node) => node.protocol))];
  const regions = [...new Set(appState.nodes.map((node) => node.region))];
  appState.filters.protocol = setSelectOptions($("#protocolFilter"), protocols, appState.filters.protocol, "全部协议");
  appState.filters.region = setSelectOptions($("#regionFilter"), regions, appState.filters.region, "全部地区");
  const sortSelect = $("#nodeSort");
  if (sortSelect) sortSelect.value = appState.filters.sort || "name";

  const subSelect = $("#subscriptionFilter");
  if (subSelect) {
    const options = [
      '<option value="all">全部订阅</option>',
      ...(appState.subscriptions || []).map(
        (sub) =>
          `<option value="${escapeHtml(sub.id)}">${escapeHtml(sub.name || "未命名订阅")} (${Number(sub.node_count || (sub.node_ids || []).length || 0)})</option>`
      ),
      '<option value="__other__">其他 / 未归组</option>',
    ];
    subSelect.innerHTML = options.join("");
    const allowed = new Set(["all", "__other__", ...(appState.subscriptions || []).map((s) => s.id)]);
    if (!allowed.has(appState.filters.subscription)) {
      appState.filters.subscription = "all";
    }
    subSelect.value = appState.filters.subscription;
  }

  $("#searchInput").value = appState.filters.search;
}

function bridgeForNode(nodeId) {
  return appState.bridges.find((bridge) => bridge.node_id === nodeId && ["running", "starting"].includes(bridge.status));
}

function renderLogFilters() {
  const select = $("#logNodeFilter");
  const nodeOptions = new Map();
  for (const node of appState.nodes || []) {
    if (node.id) nodeOptions.set(node.id, node.name || node.id);
  }
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
  return appState.requestLogs || [];
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
  const age = result.age_seconds == null ? "未知" : `${formatDuration(result.age_seconds)}前`;
  const successRate = `${Math.round(Number(result.success_rate || 0) * 100)}%`;
  return `
    <div class="test-result ${result.stale ? "is-stale" : ""}">
      <span class="test-line ${ok ? "ok" : "bad"}">${ok ? latency : "失败"}</span>
      <span class="test-message">${escapeHtml(result.message || "")}</span>
      <span class="test-result-meta">${result.stale ? "已过期 · " : ""}${escapeHtml(age)} · 成功率 ${successRate} (${Number(result.success_count || 0)}/${Number(result.attempt_count || 0)})</span>
      <button class="row-button" data-action="test" data-node="${escapeHtml(nodeId)}"><span>重测</span></button>
    </div>
  `;
}

function filteredNodes() {
  const search = appState.filters.search.trim().toLowerCase();
  const subFilter = appState.filters.subscription || "all";
  const byNode = subscriptionIndexByNodeId();
  const nodes = appState.nodes.filter((node) => {
    if (appState.filters.protocol !== "all" && node.protocol !== appState.filters.protocol) return false;
    if (appState.filters.region !== "all" && node.region !== appState.filters.region) return false;
    if (subFilter !== "all") {
      const sub = byNode.get(node.id);
      if (subFilter === "__other__") {
        if (sub) return false;
      } else if (!sub || sub.id !== subFilter) {
        return false;
      }
    }
    if (!search) return true;
    const subName = byNode.get(node.id)?.name || "";
    return [node.name, node.server, node.source, node.protocol, node.region, subName].some((value) =>
      String(value || "").toLowerCase().includes(search)
    );
  });
  const testFor = (node) => appState.tests[node.id] || {};
  return nodes.sort((left, right) => {
    if (appState.filters.sort === "latency") {
      return Number(testFor(left).latency_ms ?? Number.MAX_SAFE_INTEGER) - Number(testFor(right).latency_ms ?? Number.MAX_SAFE_INTEGER);
    }
    if (appState.filters.sort === "success_rate") {
      return Number(testFor(right).success_rate ?? -1) - Number(testFor(left).success_rate ?? -1);
    }
    if (appState.filters.sort === "tested_at") {
      return Number(testFor(right).tested_at || 0) - Number(testFor(left).tested_at || 0);
    }
    return String(left.name || "").localeCompare(String(right.name || ""), "zh-CN");
  });
}

/**
 * 将可见节点按订阅分组：优先用 subscription.node_ids；
 * 未挂在任何订阅下的节点按 source 归到「其他」分组。
 */
function groupNodesBySubscription(nodes) {
  const byNode = subscriptionIndexByNodeId();
  const groups = new Map();

  for (const sub of appState.subscriptions || []) {
    groups.set(`sub:${sub.id}`, {
      id: `sub:${sub.id}`,
      name: sub.name || "未命名订阅",
      kind: "subscription",
      subscription: sub,
      nodes: [],
    });
  }

  for (const node of nodes) {
    const sub = byNode.get(node.id);
    let group;
    if (sub) {
      const key = `sub:${sub.id}`;
      group = groups.get(key);
      if (!group) {
        group = {
          id: key,
          name: sub.name || "未命名订阅",
          kind: "subscription",
          subscription: sub,
          nodes: [],
        };
        groups.set(key, group);
      }
    } else {
      const source = String(node.source || "").trim() || "其他节点";
      const key = `source:${source}`;
      group = groups.get(key);
      if (!group) {
        group = {
          id: key,
          name: source,
          kind: "source",
          subscription: null,
          nodes: [],
        };
        groups.set(key, group);
      }
    }
    group.nodes.push(node);
  }

  const ordered = [];
  for (const sub of appState.subscriptions || []) {
    const group = groups.get(`sub:${sub.id}`);
    if (group && group.nodes.length) ordered.push(group);
  }
  const extras = [...groups.values()]
    .filter((group) => group.kind === "source" && group.nodes.length)
    .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  ordered.push(...extras);
  return ordered;
}

function renderNodeRow(node) {
  const bridge = bridgeForNode(node.id);
  const browserInstances = browserEnabled() ? browserInstancesForNode(node.id) : [];
  const runningBrowserCount = browserEnabled() ? runningBrowserInstancesForNode(node.id).length : 0;
  const browserActive = browserEnabled()
    && (runningBrowserCount > 0 || appState.active_browsers.includes(node.id));
  const transport = node.transport || "-";
  const security = node.security || "";
  const server = `${node.server}:${node.port}`;
  const recordLabel = browserInstances.length ? `浏览器记录 ${browserInstances.length}` : "浏览器记录";
  const coreOptions = Array.isArray(node.core_options) && node.core_options.length
    ? node.core_options
    : [node.preferred_core || "sing-box"];
  const coreControl = coreOptions.length > 1
    ? `<div class="node-core-control">
        <select data-core-select="${escapeHtml(node.id)}" aria-label="${escapeHtml(node.name)} 首选核心" ${bridge ? "disabled" : ""}>
          ${coreOptions.map((core) => `<option value="${escapeHtml(core)}" ${core === node.preferred_core ? "selected" : ""}>${escapeHtml(core)}</option>`).join("")}
        </select>
        <label title="首选核心启动失败时尝试备用核心">
          <input type="checkbox" data-core-fallback="${escapeHtml(node.id)}" ${node.fallback_enabled ? "checked" : ""} ${bridge ? "disabled" : ""}>
          <span>失败回退</span>
        </label>
      </div>`
    : `<span class="tag core-tag" title="当前节点仅兼容此核心">${escapeHtml(coreOptions[0])}</span>`;
  const browserActionLabel = remoteBrowserEnabled() ? "远程浏览器" : "浏览器";
  const browserActionTitle = remoteBrowserEnabled()
    ? "新开远程浏览器：独立 Docker Worker + 独立 Profile + 绑定该节点代理"
    : "新开不同指纹浏览器：独立 Profile + 绑定该节点代理";
  const browserStopTitle = remoteBrowserEnabled()
    ? "停止该节点的远程浏览器 Worker，保留本地代理"
    : "结束该节点的指纹浏览器进程，保留本地代理";
  const browserActions = browserEnabled()
    ? `
          <button class="row-button" data-action="launch-browser" data-node="${escapeHtml(node.id)}" aria-label="新开${escapeHtml(browserActionLabel)} ${escapeHtml(node.name)}" title="${escapeHtml(browserActionTitle)}">${icon("browser")}<span>${escapeHtml(browserActionLabel)}</span></button>
          <button class="row-button" data-action="browser-records" data-node="${escapeHtml(node.id)}" aria-label="查看 ${escapeHtml(node.name)} 的浏览器记录" title="浏览器记录">${icon("list")}<span>${escapeHtml(recordLabel)}</span></button>
          ${
            browserActive
              ? `<button class="row-button danger" data-action="stop-browser" data-node="${escapeHtml(node.id)}" aria-label="停止 ${escapeHtml(node.name)} 的浏览器" title="${escapeHtml(browserStopTitle)}">${icon("stop")}<span>停止浏览器${runningBrowserCount > 1 ? ` ${runningBrowserCount}` : ""}</span></button>`
              : ""
          }
        `
    : "";
  // 检查该节点是否被选中
  const isSelected = appState.selectedNodeIds.has(node.id);
  return `
    <tr class="node-row ${bridge ? "is-running" : ""} ${isSelected ? "is-selected" : ""}" data-node-id="${escapeHtml(node.id)}">
      <td class="checkbox-col">
        <label class="checkbox-wrapper" title="${isSelected ? "取消选择" : "选择"} ${escapeHtml(node.name)}">
          <input type="checkbox" class="node-checkbox" data-node-id="${escapeHtml(node.id)}" ${isSelected ? "checked" : ""} aria-label="选择 ${escapeHtml(node.name)}">
          <span class="checkbox-custom" aria-hidden="true"></span>
        </label>
      </td>
      <td>
        <div class="name-cell">
          <span class="node-name">${escapeHtml(node.name)}</span>
        </div>
      </td>
      <td><span class="region-cell">${escapeHtml(node.region || "未分类")}</span></td>
      <td><span class="tag protocol-tag">${escapeHtml(node.protocol)}</span></td>
      <td><span class="transport-tag"><strong>${escapeHtml(transport)}</strong><em>${escapeHtml(security)}</em></span></td>
      <td><span class="mono server-cell" title="${escapeHtml(server)}">${escapeHtml(server)}</span></td>
      <td>${coreControl}</td>
      <td>${testSummary(node.id)}</td>
      <td>
        <div class="row-actions">
          ${
            bridge
              ? `<button class="row-button" data-action="copy" data-text="${escapeHtml(bridge.socks_url)}">${icon("copy")}<span>SOCKS</span></button>
                <button class="row-button" data-action="copy" data-text="${escapeHtml(bridge.http_url || bridge.socks_url)}">${icon("copy")}<span>HTTP</span></button>`
              : `<button class="row-button primary-row" data-action="start" data-node="${escapeHtml(node.id)}">${icon("play")}<span>启动并复制</span></button>`
          }
          ${browserActions}
        </div>
      </td>
    </tr>
  `;
}

function renderNodeGroupHeader(group, { forceExpand = false } = {}) {
  const collapsed = !forceExpand && isNodeGroupCollapsed(group.id);
  const runningCount = group.nodes.filter((node) => bridgeForNode(node.id)).length;
  const kindLabel = group.kind === "subscription" ? "订阅" : "来源";
  const metaBits = [];
  if (group.subscription?.url) metaBits.push("URL 订阅");
  else if (group.kind === "source") metaBits.push("手动/未挂接");
  const meta = metaBits.join(" · ");
  return `
    <tr class="node-group-header ${collapsed ? "is-collapsed" : ""}" data-group-id="${escapeHtml(group.id)}">
      <td colspan="9">
        <button
          type="button"
          class="node-group-toggle"
          data-action="toggle-node-group"
          data-group="${escapeHtml(group.id)}"
          aria-expanded="${collapsed ? "false" : "true"}"
          title="${collapsed ? "展开分组" : "折叠分组"}"
        >
          <span class="node-group-chevron" aria-hidden="true">${icon("chevron")}</span>
          <span class="node-group-avatar" aria-hidden="true">${escapeHtml(initials(group.name))}</span>
          <span class="node-group-text">
            <span class="node-group-title-row">
              <strong class="node-group-name">${escapeHtml(group.name)}</strong>
              <span class="tag node-group-kind">${escapeHtml(kindLabel)}</span>
            </span>
            <span class="node-group-meta">
              ${group.nodes.length} 个节点${runningCount ? ` · ${runningCount} 运行中` : ""}${meta ? ` · ${escapeHtml(meta)}` : ""}
            </span>
          </span>
      </td>
    </tr>
  `;
}

function renderNodes() {
  const rows = $("#nodeRows");
  const empty = $("#emptyNodes");
  const nodes = filteredNodes();
  const groups = groupNodesBySubscription(nodes);
  renderSpeedTestTaskControls();
  const groupCount = groups.length;
  $("#nodeCount").textContent =
    groupCount > 1
      ? `${nodes.length} / ${appState.nodes.length} 个节点 · ${groupCount} 组`
      : `${nodes.length} / ${appState.nodes.length} 个节点`;
  empty.classList.toggle("visible", appState.nodes.length === 0);
  if (!nodes.length) {
    rows.innerHTML = "";
    if (appState.nodes.length) {
      empty.textContent = "没有匹配的节点";
      empty.classList.add("visible");
    } else {
      empty.textContent = "暂无节点";
    }
    updateBatchActionsBar();
    return;
  }
  empty.classList.remove("visible");

  // 搜索时强制展开全部分组，避免命中节点被藏在折叠里。
  const forceExpand = Boolean(appState.filters.search.trim());
  // 仅一组时仍显示分组头，便于识别订阅来源；折叠逻辑照常。
  rows.innerHTML = groups
    .map((group) => {
      const collapsed = !forceExpand && isNodeGroupCollapsed(group.id);
      const header = renderNodeGroupHeader(group, { forceExpand });
      if (collapsed) return header;
      return header + group.nodes.map((node) => renderNodeRow(node)).join("");
    })
    .join("");

  // 更新批量操作条的可见性
  updateBatchActionsBar();
  // 绑定复选框事件
  bindNodeCheckboxes();
}

// 绑定节点行的复选框事件
function bindNodeCheckboxes() {
  // 处理单个节点复选框
  document.querySelectorAll(".node-checkbox").forEach((checkbox) => {
    checkbox.addEventListener("change", (e) => {
      const nodeId = e.target.dataset.nodeId;
      if (e.target.checked) {
        appState.selectedNodeIds.add(nodeId);
      } else {
        appState.selectedNodeIds.delete(nodeId);
      }
      updateBatchActionsBar();
      // 更新行样式
      const row = document.querySelector(`[data-node-id="${nodeId}"]`);
      if (row) {
        row.classList.toggle("is-selected", e.target.checked);
      }
    });
  });

  // 处理全选复选框
  const selectAllCheckbox = $("#nodeSelectAll");
  if (selectAllCheckbox) {
    selectAllCheckbox.addEventListener("change", (e) => {
      const visibleNodes = filteredNodes();
      if (e.target.checked) {
        // 全选所有可见节点
        visibleNodes.forEach((node) => {
          appState.selectedNodeIds.add(node.id);
        });
      } else {
        // 取消全选
        visibleNodes.forEach((node) => {
          appState.selectedNodeIds.delete(node.id);
        });
      }
      // 更新所有行的复选框和样式
      document.querySelectorAll(".node-checkbox").forEach((checkbox) => {
        const nodeId = checkbox.dataset.nodeId;
        const isSelected = appState.selectedNodeIds.has(nodeId);
        checkbox.checked = isSelected;
        const row = document.querySelector(`[data-node-id="${nodeId}"]`);
        if (row) {
          row.classList.toggle("is-selected", isSelected);
        }
      });
      updateBatchActionsBar();
    });
  }
}

// 更新批量操作条的显示状态
function updateBatchActionsBar() {
  const batchBar = $("#batchActionsBar");
  if (!batchBar) return;

  const selectedCount = appState.selectedNodeIds.size;
  const selectAllCheckbox = $("#nodeSelectAll");
  const visibleNodes = filteredNodes();

  // 更新全选复选框的状态
  if (selectAllCheckbox) {
    if (visibleNodes.length === 0) {
      selectAllCheckbox.checked = false;
      selectAllCheckbox.indeterminate = false;
    } else if (selectedCount === 0) {
      selectAllCheckbox.checked = false;
      selectAllCheckbox.indeterminate = false;
    } else if (selectedCount === visibleNodes.length) {
      selectAllCheckbox.checked = true;
      selectAllCheckbox.indeterminate = false;
    } else {
      selectAllCheckbox.indeterminate = true;
    }
  }

  // 显示/隐藏批量操作条
  if (selectedCount > 0) {
    batchBar.hidden = false;
    const countEl = $("#batchSelectedCount");
    if (countEl) {
      countEl.textContent = `已选择 ${selectedCount} 个节点`;
    }
  } else {
    batchBar.hidden = true;
  }
}

// 清除所有选择
function clearNodeSelections() {
  appState.selectedNodeIds.clear();
  const selectAllCheckbox = $("#nodeSelectAll");
  if (selectAllCheckbox) {
    selectAllCheckbox.checked = false;
    selectAllCheckbox.indeterminate = false;
  }
  document.querySelectorAll(".node-checkbox").forEach((checkbox) => {
    checkbox.checked = false;
  });
  document.querySelectorAll(".node-row.is-selected").forEach((row) => {
    row.classList.remove("is-selected");
  });
  updateBatchActionsBar();
}

// 批量启动选中的节点
async function batchStartNodes() {
  if (appState.selectedNodeIds.size === 0) {
    showToast("请先选择要启动的节点", "error");
    return;
  }

  const selectedNodeIds = Array.from(appState.selectedNodeIds);
  const successCount = { count: 0 };
  const failureCount = { count: 0 };

  // 依次启动每个选中的节点
  for (const nodeId of selectedNodeIds) {
    try {
      await api("/api/node/start", { node_id: nodeId });
      successCount.count++;
    } catch (error) {
      console.error(`启动节点 ${nodeId} 失败:`, error);
      failureCount.count++;
    }
    // 添加小延迟避免过快请求
    await new Promise(resolve => setTimeout(resolve, 100));
  }

  // 刷新状态
  await loadStatus({ forceFresh: true }).catch((error) => {
    console.error("刷新状态失败:", error);
  });

  // 显示结果提示
  if (failureCount.count === 0) {
    showToast(`成功启动 ${successCount.count} 个节点`);
  } else if (successCount.count === 0) {
    showToast(`启动 ${selectedNodeIds.length} 个节点失败`, "error");
  } else {
    showToast(`成功启动 ${successCount.count} 个，失败 ${failureCount.count} 个`);
  }

  // 清除选择
  clearNodeSelections();
}

function activeSpeedTestTask() {
  const tasks = Array.isArray(appState.speedTest?.tasks) ? appState.speedTest.tasks : [];
  return [...tasks].reverse().find((task) => ["queued", "running", "cancelling"].includes(task.status)) || null;
}

function renderSpeedTestTaskControls() {
  const task = activeSpeedTestTask();
  const startButton = $("#testVisible");
  const cancelButton = $("#cancelVisibleTest");
  if (!startButton || !cancelButton) return;
  startButton.disabled = Boolean(task);
  startButton.querySelector("span").textContent = task
    ? `测速中 ${Number(task.completed || 0)}/${Number(task.total || 0)}`
    : "测试可见节点";
  cancelButton.hidden = !task;
  cancelButton.disabled = task?.status === "cancelling";
  cancelButton.dataset.taskId = task?.id || "";
}

function renderPool() {
  const pool = appState.pool || {};
  const links = pool.links || {};
  const countEl = $("#poolMemberCount");
  if (countEl) {
    const n = Number(pool.member_count || 0);
    countEl.textContent = `${n} 个成员`;
  }
  const listBtn = $("#poolCopyListJson");
  const txtBtn = $("#poolCopyProxiesTxt");
  const getBtn = $("#poolCopyGet");
  if (listBtn) listBtn.dataset.text = links.list_json || "";
  if (txtBtn) txtBtn.dataset.text = links.proxies_txt || "";
  if (getBtn) getBtn.dataset.text = links.get_health || links.get_random || "";

  const rows = $("#poolLinkRows");
  if (!rows) return;
  const items = [
    ["JSON 列表", links.list_json],
    ["文本列表", links.proxies_txt],
    ["健康优先", links.get_health || links.get_random],
  ].filter(([, url]) => url);
  if (!items.length) {
    rows.innerHTML = `<div class="pool-link-empty">启动本地代理后，这里会显示可复制的代理池链接。</div>`;
  } else rows.innerHTML = items
    .map(
      ([label, url]) => `
      <div class="pool-link-row">
        <span class="pool-link-label">${escapeHtml(label)}</span>
        <code class="pool-link-url" title="${escapeHtml(url)}">${escapeHtml(url)}</code>
      </div>`
    )
    .join("");

  const memberList = $("#poolMemberList");
  if (!memberList) return;
  const members = Array.isArray(pool.members) ? pool.members : [];
  if (!members.length) {
    memberList.innerHTML = `<div class="pool-link-empty">暂无代理池成员</div>`;
  } else {
    memberList.innerHTML = members.map((member) => {
      const state = member.circuit_state || "closed";
      const label = state === "excluded" ? "已排除" : state === "open" ? "熔断" : state === "half_open" ? "恢复探测" : "健康";
      const latency = member.latency_ms == null ? "未测速" : `${Number(member.latency_ms)} ms`;
      const action = state === "excluded"
        ? `<button class="row-button" type="button" data-action="exclude-pool-member" data-id="${escapeHtml(member.id || "")}" data-duration="0">恢复</button>`
        : `<button class="row-button" type="button" data-action="exclude-pool-member" data-id="${escapeHtml(member.id || "")}" data-duration="1800">排除 30 分钟</button>`;
      return `
        <div class="pool-member-row is-${escapeHtml(state)}">
          <div>
            <strong>${escapeHtml(member.node_name || "未命名节点")}</strong>
            <span>${escapeHtml(label)} · 健康 ${Number(member.health_score || 0)} · ${escapeHtml(latency)}</span>
          </div>
          ${action}
        </div>`;
    }).join("");
  }
  renderSmartGateway();
}

function renderSmartGateway() {
  const gateway = appState.smartGateway || {};
  const card = $("#smartGatewayCard");
  const status = $("#smartGatewayStatus");
  const strategy = $("#smartGatewayStrategy");
  const port = $("#smartGatewayPort");
  const region = $("#smartGatewayRegion");
  const query = $("#smartGatewayQuery");
  const save = $("#smartGatewaySave");
  const stop = $("#smartGatewayStop");
  if (!status || !strategy || !port || !region || !query || !save || !stop) return;
  strategy.value = gateway.strategy || "health";
  if (document.activeElement !== port) port.value = Number(gateway.port || 21000);
  if (document.activeElement !== region) region.value = gateway.region || "";
  if (document.activeElement !== query) query.value = gateway.query || "";
  const links = $("#smartGatewayLinks");
  const unavailable = dockerRuntime();
  card?.classList.toggle("is-unavailable", unavailable);
  card?.setAttribute("aria-disabled", unavailable ? "true" : "false");
  [strategy, port, region, query].forEach((control) => { control.disabled = unavailable; });
  if (unavailable) {
    status.textContent = "Docker 模式不可用";
    save.textContent = "Docker 不可用";
    save.disabled = true;
    stop.disabled = true;
    if (links) links.innerHTML = '<div class="pool-link-empty">Docker 未发布智能网关固定端口，请在宿主机模式使用。</div>';
    return;
  }
  status.textContent = gateway.running ? `运行中 · ${Number(gateway.available_members || 0)} 个节点` : (gateway.last_error || "未运行");
  save.textContent = gateway.running ? "保存设置" : "保存并启动";
  save.disabled = false;
  stop.disabled = !gateway.running;
  if (links) {
    links.innerHTML = gateway.running
      ? `<div class="pool-link-row"><span class="pool-link-label">SOCKS5</span><code class="pool-link-url">${escapeHtml(gateway.socks_url || "")}</code></div><div class="pool-link-row"><span class="pool-link-label">HTTP</span><code class="pool-link-url">${escapeHtml(gateway.http_url || "")}</code></div>`
      : `<div class="pool-link-empty">保存设置后启动固定入口。</div>`;
  }
}

function renderBridges() {
  const list = $("#bridgeList");
  const bridges = runningBridges();
  const count = $("#bridgesCount");
  if (count) count.textContent = `${bridges.length} 个运行中`;
  $("#stopAll").disabled = bridges.length === 0 && !appState.smartGateway?.running;

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
    const listenAddress = bridge.listen_host || endpoint.address;
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
              <span class="endpoint-value">${escapeHtml(listenAddress)}</span>
            </div>
            <button class="row-button copy-icon" data-action="copy" data-text="${escapeHtml(listenAddress)}" aria-label="复制监听地址" title="复制监听地址">${icon("copy")}</button>
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
  const page = appState.requestLogPage;
  $("#logCount").textContent = page.loaded && page.filtered_total !== page.total
    ? `${page.filtered_total} / ${page.total} 条`
    : `${page.loaded ? page.total : allLogs.length} 条`;
  $("#requestLogPage").textContent = `第 ${page.page} / ${page.page_count} 页`;
  $("#requestLogPrev").disabled = page.page <= 1;
  $("#requestLogNext").disabled = page.page >= page.page_count;
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
  return appState.systemLogs || [];
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
  const page = appState.systemLogPage;
  $("#systemCount").textContent = page.loaded && page.filtered_total !== page.total
    ? `${page.filtered_total} / ${page.total} 条`
    : `${page.loaded ? page.total : all.length} 条`;
  $("#systemLogPage").textContent = `第 ${page.page} / ${page.page_count} 页`;
  $("#systemLogPrev").disabled = page.page <= 1;
  $("#systemLogNext").disabled = page.page >= page.page_count;
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

function openModal(modal) {
  modal.hidden = false;
  const shell = $(".app-shell");
  if (shell) {
    shell.inert = true;
    shell.setAttribute("aria-hidden", "true");
  }
}

function closeModal(modal) {
  modal.hidden = true;
  if (
    $("#coreLogModal").hidden
    && $("#coreUploadModal").hidden
    && $("#browserRecordModal").hidden
    && $("#remoteBrowserLaunchModal").hidden
    && $("#remoteBrowserCredentialsModal").hidden
  ) {
    const shell = $(".app-shell");
    if (shell) {
      shell.inert = false;
      shell.removeAttribute("aria-hidden");
    }
  }
}

function findActionTrigger(key, action = "") {
  if (!key) return null;
  return [...document.querySelectorAll("button[data-action]")].find((button) => (
    actionLockKey(button) === key && (!action || button.dataset.action === action)
  )) || null;
}

function closeCoreLogModal() {
  coreLogRequestGeneration += 1;
  coreLogAbortController?.abort();
  coreLogAbortController = null;
  closeModal($("#coreLogModal"));
  const trigger = lastCoreLogTrigger?.isConnected ? lastCoreLogTrigger : findActionTrigger(lastCoreLogTriggerKey);
  if (trigger && !trigger.disabled) {
    trigger.focus();
    pendingModalFocusKey = null;
  } else {
    pendingModalFocusKey = lastCoreLogTriggerKey;
  }
  lastCoreLogTrigger = null;
  lastCoreLogTriggerKey = null;
}

function showCoreUploadError(message = "") {
  const element = $("#coreUploadError");
  element.textContent = message;
  element.hidden = !message;
}

function coreUploadAccept(kind) {
  if (kind === "sing-box") return ".tar.gz,.tgz,.zip";
  if (kind === "xray") return ".zip";
  return ".zip,.tar.gz,.tgz,.tar.xz,.tar,.dmg";
}

function coreUploadExample(kind) {
  const os = appState.platform?.os || "linux";
  const hostArch = appState.platform?.arch || "x86_64";
  const arch = kind === "fingerprint-chromium" && remoteBrowserEnabled() ? "x86_64" : hostArch;
  if (kind === "sing-box") {
    const osName = os === "darwin" ? "darwin" : os === "windows" ? "windows" : "linux";
    const archName = arch === "arm64" ? "arm64" : "amd64";
    const suffix = os === "windows" ? ".zip" : ".tar.gz";
    return `例如 sing-box-版本-${osName}-${archName}${suffix}`;
  }
  if (kind === "xray") {
    const osName = os === "darwin" ? "macos" : os === "windows" ? "windows" : "linux";
    const archName = arch === "arm64" ? "arm64-v8a" : "64";
    return `例如 Xray-${osName}-${archName}.zip`;
  }
  const platformName = remoteBrowserEnabled()
    ? "linux"
    : appState.platform?.fingerprint_platform || os;
  const archName = arch === "arm64" ? "arm64" : "x64";
  return `例如 fingerprint-chromium_${platformName}_${archName}.zip（以实际上游文件名为准）`;
}

function updateCoreUploadControls({ clearFile = false } = {}) {
  const kindSelect = $("#coreUploadKind");
  const fingerprintOption = kindSelect.querySelector('option[value="fingerprint-chromium"]');
  fingerprintOption.disabled = !browserEnabled();
  if (fingerprintOption.disabled && kindSelect.value === "fingerprint-chromium") {
    kindSelect.value = "sing-box";
  }
  const kind = kindSelect.value;
  const fileInput = $("#coreUploadFile");
  fileInput.accept = coreUploadAccept(kind);
  if (clearFile) fileInput.value = "";
  $("#coreUploadPlatformHint").textContent = coreUploadClient.platformHint(
    kind,
    appState.platform || {},
    remoteBrowserEnabled(),
  );
  const idleHint = kind === "fingerprint-chromium"
    ? "替换前请停止所有浏览器实例。"
    : `替换前请停止正在使用 ${kind} 的本地代理。`;
  $("#coreUploadFileHint").textContent = `${coreUploadExample(kind)}。${idleHint}`;
  $("#coreUploadMeta").textContent = "原始文件流式上传 · 单文件最大 2 GiB · 解压后最大 4 GiB";
}

function setCoreUploadBusy(busy) {
  const modal = $("#coreUploadModal");
  modal.querySelectorAll("button, input, select").forEach((control) => {
    control.disabled = busy;
  });
  const submit = $("#coreUploadSubmit");
  submit.classList.toggle("is-busy", busy);
  submit.setAttribute("aria-busy", busy ? "true" : "false");
  submit.querySelector("span").textContent = busy ? "正在上传并安装…" : "上传并安装";
  if (busy) {
    const file = $("#coreUploadFile").files?.[0];
    $("#coreUploadMeta").textContent = file
      ? `正在处理 ${file.name} · ${formatBytes(file.size)}`
      : "正在上传并安装";
  } else {
    updateCoreUploadControls();
  }
}

function showCoreUploadModal(trigger = null) {
  if (!coreUploadClient) throw new Error("本地导入模块未加载，请刷新页面后重试");
  const modal = $("#coreUploadModal");
  $("#coreUploadForm").reset();
  showCoreUploadError();
  updateCoreUploadControls();
  setCoreUploadBusy(false);
  lastCoreUploadTrigger = trigger;
  lastCoreUploadTriggerKey = trigger ? actionLockKey(trigger) : null;
  openModal(modal);
  window.requestAnimationFrame(() => {
    if (!modal.hidden) $("#coreUploadKind")?.focus();
  });
}

function closeCoreUploadModal({ force = false } = {}) {
  if (coreUploadPromise && !force) return false;
  const modal = $("#coreUploadModal");
  if (!modal || modal.hidden) return false;
  closeModal(modal);
  $("#coreUploadForm").reset();
  showCoreUploadError();
  const trigger = lastCoreUploadTrigger?.isConnected
    ? lastCoreUploadTrigger
    : findActionTrigger(lastCoreUploadTriggerKey, "show-core-upload");
  trigger?.focus();
  lastCoreUploadTrigger = null;
  lastCoreUploadTriggerKey = null;
  return true;
}

async function handleCoreUploadSubmit(event) {
  event.preventDefault();
  if (coreUploadPromise) return;
  const kind = $("#coreUploadKind").value;
  const file = $("#coreUploadFile").files?.[0];
  const sha256 = $("#coreUploadSha256").value;
  showCoreUploadError();

  const upload = coreUploadClient.uploadKernel({
    fetchImpl: window.fetch.bind(window),
    auth: managementAuth,
    browser: window,
    token: managementToken,
    kind,
    file,
    sha256,
  });
  coreUploadPromise = upload;
  setCoreUploadBusy(true);
  renderCores();

  let result = null;
  let statusWarning = "";
  try {
    result = await upload;
    try {
      await loadStatus({ forceFresh: true });
    } catch (error) {
      statusWarning = `，但状态刷新失败：${error.message}`;
    }
  } catch (error) {
    if (error?.code === "AUTH_INVALID") managementToken = "";
    const message = `本地导入失败：${error.message}`;
    showCoreUploadError(message);
    showToast(message, "error");
  } finally {
    coreUploadPromise = null;
    setCoreUploadBusy(false);
    renderCores();
  }

  if (!result) return;
  closeCoreUploadModal();
  const digest = String(result.sha256 || "");
  const digestLabel = digest ? ` · SHA-256 ${digest.slice(0, 12)}…` : "";
  const successMessage = `${kind} 本地导入完成${digestLabel}${statusWarning}`;
  showToast(result.arch_warning || successMessage, result.arch_warning || statusWarning ? "error" : "ok");
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
  coreLogAbortController?.abort();
  const controller = new AbortController();
  const generation = ++coreLogRequestGeneration;
  coreLogAbortController = controller;
  const modal = $("#coreLogModal");
  openModal(modal);
  modal.querySelector('[data-action="close-core-log"]')?.focus();
  $("#coreLogTitle").textContent = `${core} 运行日志`;
  $("#coreLogMeta").textContent = "加载中";
  $("#coreLogBody").innerHTML = '<div class="core-log-loading">正在读取日志...</div>';
  try {
    const payload = await api(`/api/core/logs?core=${encodeURIComponent(core)}`, null, { signal: controller.signal });
    if (generation !== coreLogRequestGeneration || modal.hidden) return;
    renderCoreLogPayload(payload);
  } catch (error) {
    if (generation !== coreLogRequestGeneration || error?.name === "AbortError" || modal.hidden) return;
    $("#coreLogMeta").textContent = "读取失败";
    $("#coreLogBody").innerHTML = `<div class="core-log-empty error"><strong>日志读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  } finally {
    if (coreLogAbortController === controller) coreLogAbortController = null;
  }
}

function closeBrowserRecordModal() {
  closeModal($("#browserRecordModal"));
  appState.browserRecordNodeId = null;
  const trigger = lastBrowserRecordTrigger?.isConnected ? lastBrowserRecordTrigger : findActionTrigger(lastBrowserRecordTriggerKey);
  trigger?.focus();
  lastBrowserRecordTrigger = null;
  lastBrowserRecordTriggerKey = null;
}

function renderRemoteBrowserBrokerStatus() {
  const element = $("#remoteBrowserBrokerStatus");
  if (!element) return;
  const broker = appState.remoteBrowser || {};
  const available = broker.configured === true && broker.available === true;
  element.classList.toggle("is-ready", available);
  element.classList.toggle("is-error", !available);
  if (available) {
    const count = Number(broker.active_sessions || 0);
    const imagePending = broker.worker_image_ready === false;
    const image = String(broker.worker_image || "lscr.io/linuxserver/chromium:latest");
    element.textContent = imagePending
      ? `浏览器 Broker 已连接 · Worker 镜像尚未缓存，首次启动需拉取 ${image}`
      : `浏览器 Broker 已连接 · ${count} 个 Worker 运行中`;
    element.setAttribute("role", "status");
    return;
  }
  const message = String(broker.error || "浏览器 Broker 尚未就绪，请检查 Docker 服务状态").trim();
  element.textContent = `浏览器 Broker 不可用：${message}`;
  element.setAttribute("role", "alert");
}

function showRemoteBrowserLaunchError(message = "") {
  const element = $("#remoteBrowserLaunchError");
  if (!element) return;
  const text = String(message || "").trim();
  element.textContent = text;
  element.hidden = !text;
}

function generateRemoteBrowserPassword() {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const cryptoApi = window.crypto;
  if (!cryptoApi?.getRandomValues) return "";
  const bytes = new Uint8Array(20);
  cryptoApi.getRandomValues(bytes);
  return Array.from(bytes, (value) => alphabet[value & 63]).join("");
}

function fillGeneratedRemoteBrowserPassword({ announce = false } = {}) {
  const password = generateRemoteBrowserPassword();
  if (!password) {
    showRemoteBrowserLaunchError("当前浏览器无法安全生成随机密码，请手动填写强密码");
    return false;
  }
  $("#remoteBrowserPassword").value = password;
  $("#remoteBrowserPasswordConfirm").value = password;
  showRemoteBrowserLaunchError();
  if (announce) showToast("已生成新的远程浏览器密码");
  return true;
}

async function copyRemoteBrowserPassword() {
  const password = String($("#remoteBrowserPassword")?.value || "");
  if (!password) throw new Error("当前没有可复制的远程浏览器密码");
  if (!await browserUi.copyText(password, window)) {
    throw new Error("当前环境无法复制密码，请手动保存");
  }
  showToast("已复制远程浏览器密码");
}

function remoteBrowserSuccessFields() {
  return {
    remoteUrl: $("#remoteBrowserSuccessUrl"),
    username: $("#remoteBrowserSuccessUsername"),
    password: $("#remoteBrowserSuccessPassword"),
  };
}

function remoteBrowserCredentialView() {
  return {
    setup: $("#remoteBrowserLaunchSetup"),
    success: $("#remoteBrowserLaunchSuccess"),
    dialog: $("#remoteBrowserLaunchModal")?.querySelector('[role="dialog"]'),
  };
}

function launchedRemoteBrowserCredentials() {
  const credentials = browserUi.credentialFields(remoteBrowserSuccessFields());
  credentials.remoteUrl = safeRemoteBrowserUrl(credentials.remoteUrl);
  if (!credentials.remoteUrl || !credentials.username || !credentials.password) {
    throw new Error("当前页面中的远程浏览器凭据已被清除");
  }
  return credentials;
}

function resetRemoteBrowserLaunchContent() {
  browserUi.clearCredentialFields(remoteBrowserSuccessFields());
  $("#remoteBrowserUsername").value = "";
  $("#remoteBrowserPassword").value = "";
  $("#remoteBrowserPasswordConfirm").value = "";
  $("#remoteBrowserLaunchTitle").textContent = "启动远程浏览器";
  $("#remoteBrowserLaunchMeta").textContent = "";
  browserUi.setCredentialView(remoteBrowserCredentialView(), false);
}

function showRemoteBrowserLaunchSuccess(result, credentials) {
  const remoteUrl = safeRemoteBrowserUrl(result?.remote_url);
  if (!remoteUrl) throw new Error("浏览器 Worker 未返回有效的远程访问地址");
  browserUi.setCredentialFields(remoteBrowserSuccessFields(), {
    remoteUrl,
    username: credentials.username,
    password: credentials.password,
  });
  // 成功态只保留一份凭据，避免隐藏的设置表单继续持有密码副本。
  $("#remoteBrowserUsername").value = "";
  $("#remoteBrowserPassword").value = "";
  $("#remoteBrowserPasswordConfirm").value = "";
  browserUi.setCredentialView(remoteBrowserCredentialView(), true);
  $("#remoteBrowserLaunchTitle").textContent = `${result.browser_name || "远程浏览器"} 已启动`;
  $("#remoteBrowserLaunchMeta").textContent = `${credentials.username} · ${remoteUrl}`;
}

async function copyLaunchedRemoteBrowserPassword() {
  const credentials = launchedRemoteBrowserCredentials();
  if (!await browserUi.copyText(credentials.password, window)) {
    throw new Error("当前环境无法复制密码，请手动保存");
  }
  showToast("已复制远程浏览器密码");
}

async function copyLaunchedRemoteBrowserUrl() {
  const credentials = launchedRemoteBrowserCredentials();
  if (!await browserUi.copyText(credentials.remoteUrl, window)) {
    throw new Error("当前环境无法复制远程链接，请手动选择链接");
  }
  showToast("已复制远程浏览器链接");
}

async function copyLaunchedRemoteBrowserCredentials() {
  const credentials = launchedRemoteBrowserCredentials();
  if (!await browserUi.copyText(browserUi.formatCredentials(credentials), window)) {
    throw new Error("当前环境无法复制完整凭据，请分别手动保存");
  }
  showToast("已复制远程浏览器完整凭据");
}

function openLaunchedRemoteBrowser() {
  const credentials = launchedRemoteBrowserCredentials();
  openRemoteBrowserUrl(credentials.remoteUrl);
  showToast("正在打开远程浏览器");
}

function finishRemoteBrowserLaunch() {
  if (closeRemoteBrowserLaunchModal()) {
    showToast("远程浏览器凭据已从当前页面清除");
  }
}

function setRemoteBrowserLaunchBusy(busy) {
  const modal = $("#remoteBrowserLaunchModal");
  const submit = $("#remoteBrowserLaunchSubmit");
  if (submit) {
    submit.disabled = busy;
    submit.classList.toggle("is-busy", busy);
    submit.setAttribute("aria-busy", busy ? "true" : "false");
    const label = submit.querySelector("span");
    if (label) {
      label.textContent = busy
        ? appState.remoteBrowser?.worker_image_ready === false
          ? "正在准备 Worker 镜像…"
          : "正在创建 Worker…"
        : "启动并打开";
    }
  }
  modal?.querySelectorAll([
    '[data-action="close-remote-browser-launch"]',
    '[data-action="generate-remote-browser-password"]',
    '[data-action="copy-remote-browser-password"]',
    '[data-action="copy-launched-remote-browser-password"]',
    '[data-action="copy-launched-remote-browser-url"]',
    '[data-action="copy-launched-remote-browser-credentials"]',
    '[data-action="open-launched-remote-browser"]',
    '[data-action="finish-remote-browser-launch"]',
  ].join(",")).forEach((button) => {
    button.disabled = busy;
  });
  ["#remoteBrowserUsername", "#remoteBrowserPassword", "#remoteBrowserPasswordConfirm"].forEach((selector) => {
    const input = $(selector);
    if (input) input.disabled = busy;
  });
}

function showRemoteBrowserLaunchModal(nodeId, instanceId = "", trigger = null) {
  if (!remoteBrowserEnabled()) throw new Error("当前运行模式不提供远程浏览器功能");
  const node = nodeById(nodeId);
  if (!nodeId || !node) throw new Error("找不到远程浏览器对应的节点");
  const instance = instanceId ? browserInstanceById(instanceId) : null;
  if (instanceId && !instance) throw new Error("找不到浏览器记录");

  resetRemoteBrowserLaunchContent();
  pendingRemoteBrowserLaunch = { nodeId, instanceId };
  lastRemoteBrowserLaunchTrigger = trigger;
  lastRemoteBrowserLaunchTriggerKey = trigger ? actionLockKey(trigger) : null;
  lastRemoteBrowserLaunchTriggerAction = trigger?.dataset?.action || null;
  $("#remoteBrowserLaunchTitle").textContent = instance
    ? `启动 ${instance.name || "远程浏览器"}`
    : "启动远程浏览器";
  $("#remoteBrowserLaunchMeta").textContent = `${node.name || nodeId} · ${instance ? "复用独立 Profile" : "新建独立 Worker 与 Profile"}`;
  $("#remoteBrowserUsername").value = String(instance?.remote_username || "proxybridge");
  $("#remoteBrowserPassword").value = "";
  $("#remoteBrowserPasswordConfirm").value = "";
  showRemoteBrowserLaunchError();
  fillGeneratedRemoteBrowserPassword();
  renderRemoteBrowserBrokerStatus();
  setRemoteBrowserLaunchBusy(false);

  const recordsModal = $("#browserRecordModal");
  const modal = $("#remoteBrowserLaunchModal");
  modal.dataset.returnToBrowserRecords = recordsModal.hidden ? "false" : "true";
  if (!recordsModal.hidden) {
    recordsModal.inert = true;
    recordsModal.setAttribute("aria-hidden", "true");
  }
  openModal(modal);
  window.requestAnimationFrame(() => {
    if (!modal.hidden) $("#remoteBrowserUsername")?.focus();
  });
}

function closeRemoteBrowserLaunchModal({ force = false } = {}) {
  if (remoteBrowserLaunchPromise && !force) return false;
  const modal = $("#remoteBrowserLaunchModal");
  if (!modal || modal.hidden) return false;
  closeModal(modal);
  if (modal.dataset.returnToBrowserRecords === "true") {
    const recordsModal = $("#browserRecordModal");
    recordsModal.inert = false;
    recordsModal.removeAttribute("aria-hidden");
  }
  delete modal.dataset.returnToBrowserRecords;
  pendingRemoteBrowserLaunch = null;
  showRemoteBrowserLaunchError();
  resetRemoteBrowserLaunchContent();

  const trigger = lastRemoteBrowserLaunchTrigger?.isConnected
    ? lastRemoteBrowserLaunchTrigger
    : findActionTrigger(lastRemoteBrowserLaunchTriggerKey, lastRemoteBrowserLaunchTriggerAction);
  trigger?.focus();
  lastRemoteBrowserLaunchTrigger = null;
  lastRemoteBrowserLaunchTriggerKey = null;
  lastRemoteBrowserLaunchTriggerAction = null;
  return true;
}

function viewedRemoteBrowserCredentialFields() {
  return {
    remoteUrl: $("#remoteBrowserCredentialsUrl"),
    username: $("#remoteBrowserCredentialsUsername"),
    password: $("#remoteBrowserCredentialsPassword"),
  };
}

function viewedRemoteBrowserCredentials() {
  const credentials = browserUi.credentialFields(viewedRemoteBrowserCredentialFields());
  credentials.remoteUrl = safeRemoteBrowserUrl(credentials.remoteUrl);
  if (!credentials.remoteUrl || !credentials.username || !credentials.password) {
    throw new Error("远程浏览器凭证已被清除，请重新查看");
  }
  return credentials;
}

function resetRemoteBrowserCredentialsContent() {
  browserUi.clearCredentialFields(viewedRemoteBrowserCredentialFields());
  $("#remoteBrowserCredentialsTitle").textContent = "查看远程浏览器凭证";
  $("#remoteBrowserCredentialsMeta").textContent = "";
  viewedRemoteBrowserInstanceId = null;
}

function closeRemoteBrowserCredentialsModal() {
  const modal = $("#remoteBrowserCredentialsModal");
  if (!modal || modal.hidden) return false;
  remoteBrowserCredentialsRequestGeneration += 1;
  closeModal(modal);
  modal.querySelector('[role="dialog"]')?.setAttribute("aria-busy", "false");
  if (modal.dataset.returnToBrowserRecords === "true") {
    const recordsModal = $("#browserRecordModal");
    recordsModal.inert = false;
    recordsModal.removeAttribute("aria-hidden");
  }
  delete modal.dataset.returnToBrowserRecords;
  resetRemoteBrowserCredentialsContent();

  const trigger = lastRemoteBrowserCredentialsTrigger?.isConnected
    ? lastRemoteBrowserCredentialsTrigger
    : findActionTrigger(
        lastRemoteBrowserCredentialsTriggerKey,
        lastRemoteBrowserCredentialsTriggerAction,
      );
  if (trigger && !trigger.disabled) {
    trigger.focus();
  } else {
    pendingModalFocusKey = lastRemoteBrowserCredentialsTriggerKey;
  }
  lastRemoteBrowserCredentialsTrigger = null;
  lastRemoteBrowserCredentialsTriggerKey = null;
  lastRemoteBrowserCredentialsTriggerAction = null;
  return true;
}

async function showRemoteBrowserCredentials(instanceId, trigger = null) {
  if (!remoteBrowserEnabled()) throw new Error("当前运行模式不提供远程浏览器凭证查询");
  const instance = browserInstanceById(instanceId);
  if (!instance) throw new Error("找不到远程浏览器记录");
  if (instance.status !== "running") throw new Error("仅运行中的远程浏览器可以查看访问凭证");

  const modal = $("#remoteBrowserCredentialsModal");
  const recordsModal = $("#browserRecordModal");
  resetRemoteBrowserCredentialsContent();
  viewedRemoteBrowserInstanceId = instanceId;
  const requestGeneration = ++remoteBrowserCredentialsRequestGeneration;
  lastRemoteBrowserCredentialsTrigger = trigger;
  lastRemoteBrowserCredentialsTriggerKey = trigger ? actionLockKey(trigger) : null;
  lastRemoteBrowserCredentialsTriggerAction = trigger?.dataset?.action || null;
  $("#remoteBrowserCredentialsTitle").textContent = `${instance.name || "远程浏览器"} 访问凭证`;
  $("#remoteBrowserCredentialsMeta").textContent = "正在从 Browser Broker 安全读取…";
  modal.dataset.returnToBrowserRecords = recordsModal.hidden ? "false" : "true";
  if (!recordsModal.hidden) {
    recordsModal.inert = true;
    recordsModal.setAttribute("aria-hidden", "true");
  }
  openModal(modal);
  modal.querySelector('[role="dialog"]')?.setAttribute("aria-busy", "true");
  modal.querySelector('[data-action="close-remote-browser-credentials"]')?.focus();

  try {
    const result = await api("/api/browser/credentials", { instance_id: instanceId });
    if (
      requestGeneration !== remoteBrowserCredentialsRequestGeneration
      || modal.hidden
      || viewedRemoteBrowserInstanceId !== instanceId
    ) return;
    const remoteUrl = safeRemoteBrowserUrl(result.remote_url);
    const username = String(result.username || "");
    const password = String(result.password || "");
    if (
      !remoteUrl
      || !/^[A-Za-z0-9._-]{1,32}$/.test(username)
      || Array.from(password).length < 10
      || Array.from(password).length > 128
      || /[\x00-\x1f\x7f]/.test(password)
    ) {
      throw new Error("服务器返回的远程浏览器凭证格式无效");
    }
    browserUi.setCredentialFields(viewedRemoteBrowserCredentialFields(), {
      remoteUrl,
      username,
      password,
    });
    $("#remoteBrowserCredentialsMeta").textContent = `${username} · ${remoteUrl}`;
    modal.querySelector('[role="dialog"]')?.setAttribute("aria-busy", "false");
    $("#remoteBrowserCredentialsOpen")?.focus();
  } catch (error) {
    if (
      requestGeneration !== remoteBrowserCredentialsRequestGeneration
      || modal.hidden
      || viewedRemoteBrowserInstanceId !== instanceId
    ) return;
    closeRemoteBrowserCredentialsModal();
    throw error;
  }
}

async function copyViewedRemoteBrowserPassword() {
  const credentials = viewedRemoteBrowserCredentials();
  if (!await browserUi.copyText(credentials.password, window)) {
    throw new Error("当前环境无法复制密码，请手动选择");
  }
  showToast("已复制远程浏览器密码");
}

async function copyViewedRemoteBrowserUrl() {
  const credentials = viewedRemoteBrowserCredentials();
  if (!await browserUi.copyText(credentials.remoteUrl, window)) {
    throw new Error("当前环境无法复制远程链接，请手动选择");
  }
  showToast("已复制远程浏览器链接");
}

async function copyViewedRemoteBrowserCredentials() {
  const credentials = viewedRemoteBrowserCredentials();
  if (!await browserUi.copyText(browserUi.formatCredentials(credentials), window)) {
    throw new Error("当前环境无法复制完整凭证，请分别手动选择");
  }
  showToast("已复制远程浏览器完整凭证");
}

function openViewedRemoteBrowser() {
  const credentials = viewedRemoteBrowserCredentials();
  openRemoteBrowserUrl(credentials.remoteUrl);
  showToast("正在打开远程浏览器");
}

function openRemoteBrowserUrl(value) {
  const remoteUrl = safeRemoteBrowserUrl(value);
  if (!remoteUrl) throw new Error("远程浏览器访问地址无效，请刷新状态后重试");
  const link = document.createElement("a");
  link.href = remoteUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function openRemoteBrowserInstance(instanceId) {
  const instance = browserInstanceById(instanceId);
  if (!instance) throw new Error("找不到远程浏览器记录");
  openRemoteBrowserUrl(instance.remote_url);
  showToast(`正在打开 ${instance.name || "远程浏览器"}`);
}

function compactPath(value) {
  const parts = String(value || "").split("/").filter(Boolean);
  return parts.length > 3 ? `.../${parts.slice(-3).join("/")}` : value || "-";
}

function renderBrowserRecords(nodeId) {
  const node = nodeById(nodeId);
  const instances = browserInstancesForNode(nodeId);
  const runningCount = instances.filter((instance) => ["running", "starting", "stopping"].includes(instance.status)).length;
  $("#browserRecordTitle").textContent = `${node?.name || "节点"} 浏览器记录`;
  $("#browserRecordMeta").textContent = `${instances.length} 个记录 · ${runningCount} 个运行中`;
  const body = $("#browserRecordBody");
  const brokerError = remoteBrowserEnabled() && appState.remoteBrowser?.available !== true;
  const brokerAlert = brokerError
    ? `<div class="browser-record-broker-alert" role="alert">
        <strong>浏览器 Broker 不可用</strong>
        <span>${escapeHtml(appState.remoteBrowser?.error || "请检查 Docker Broker 与 Worker 服务状态后重试")}</span>
      </div>`
    : "";
  if (!instances.length) {
    body.innerHTML = `${brokerAlert}
      <div class="core-log-empty">
        <strong>暂无浏览器记录</strong>
        <span>${escapeHtml(node?.name || nodeId || "-")}</span>
      </div>
    `;
    return;
  }

  // 状态轮询会反复 render：先保留输入草稿与光标，避免标签框刚输入就被清空。
  const draftById = {};
  body.querySelectorAll(".browser-label-input").forEach((el) => {
    const id = el.dataset.instance;
    if (id) draftById[id] = el.value;
  });
  const active = document.activeElement;
  const activeId =
    active && active.classList && active.classList.contains("browser-label-input")
      ? active.dataset.instance
      : null;
  let selStart = null;
  let selEnd = null;
  if (activeId && active && typeof active.selectionStart === "number") {
    selStart = active.selectionStart;
    selEnd = active.selectionEnd;
  }

  body.innerHTML = brokerAlert + instances
    .map((instance) => {
      const running = ["running", "starting", "stopping"].includes(instance.status);
      const remoteWorker = instance.remote_worker === true || remoteBrowserEnabled();
      const remoteUrl = safeRemoteBrowserUrl(instance.remote_url);
      const remoteUrlDisplay = String(instance.remote_url || "").trim() || "-";
      const workerId = String(instance.remote_container_id || "").slice(0, 12);
      const workerStatus = browserStatusLabel(instance.status);
      const instanceError = String(instance.error || "").trim();
      const profilePath = instance.profile_path || "";
      const action = running ? "stop-browser-instance" : "launch-browser-instance";
      const actionLabel = running ? "停止" : "启动";
      const actionIcon = running ? "stop" : "browser";
      const lastUsedAt = browserUi.browserInstanceLastUsedAt(instance);
      const savedLabel = instance.label || "";
      // Prefer in-progress draft over server value while user is editing / mid-type.
      const label =
        Object.prototype.hasOwnProperty.call(draftById, instance.id)
          ? draftById[instance.id]
          : savedLabel;
      const pillLabel = String(label || "").trim();
      return `
        <article class="browser-record-entry ${running ? "is-running" : ""}" data-instance-id="${escapeHtml(instance.id)}">
          <header>
            <div>
              <strong>${escapeHtml(instance.name || "浏览器")}</strong>
              <span>${remoteWorker ? "远程 Worker" : instance.legacy ? "默认浏览器" : "独立指纹"} · 创建 ${escapeHtml(formatTime(instance.created_at))}</span>
              ${pillLabel ? `<span class="browser-record-label-pill" title="${escapeHtml(pillLabel)}">${escapeHtml(pillLabel)}</span>` : ""}
            </div>
            <span class="status ${escapeHtml(instance.status || "stopped")}">${escapeHtml(browserStatusLabel(instance.status))}</span>
          </header>
          <div class="browser-record-grid">
            <span>指纹</span><code>${escapeHtml(instance.fingerprint_seed || "-")}</code>
            <span>Profile</span><code title="${escapeHtml(profilePath)}">${escapeHtml(compactPath(profilePath))}</code>
            <span>最近使用</span><code>${escapeHtml(formatTime(lastUsedAt))}</code>
            <span>上次启动</span><code>${escapeHtml(formatTime(instance.last_started_at))}</code>
            ${remoteWorker
              ? `<span>Worker 状态</span><code>${escapeHtml(workerStatus)}${workerId ? ` · ${escapeHtml(workerId)}` : ""}</code>
                 <span>远程端口</span><code>${escapeHtml(instance.remote_host_port || "-")}</code>
                 <span>远程地址</span><code title="${escapeHtml(remoteUrlDisplay)}">${escapeHtml(remoteUrlDisplay)}</code>
                 <span>访问用户</span><code>${escapeHtml(instance.remote_username || "-")}</code>`
              : `<span>PID</span><code>${escapeHtml(instance.pid || "-")}</code>`}
          </div>
          ${instanceError
            ? `<div class="browser-record-error" role="alert"><strong>${remoteWorker ? "Worker 错误" : "启动错误"}</strong><span>${escapeHtml(instanceError)}</span></div>`
            : ""}
          <div class="browser-record-label-row">
            <label>
              <span>标签</span>
              <input
                type="text"
                class="browser-label-input"
                maxlength="120"
                placeholder="例如：访问 xx 的浏览器"
                value="${escapeHtml(label)}"
                data-instance="${escapeHtml(instance.id)}"
                aria-label="浏览器标签 ${escapeHtml(instance.name || "")}"
              >
            </label>
            <button
              class="row-button"
              type="button"
              data-action="save-browser-label"
              data-instance="${escapeHtml(instance.id)}"
            ><span>保存标签</span></button>
          </div>
          <div class="browser-record-actions">
            ${remoteWorker
              ? `<button
                  class="row-button"
                  type="button"
                  data-action="view-remote-browser-credentials"
                  data-instance="${escapeHtml(instance.id)}"
                  aria-label="查看 ${escapeHtml(instance.name || "浏览器")} 的访问凭证"
                  title="${instance.status === "running" ? "从 Browser Broker 按需读取当前访问账号和密码" : "停止后访问密码已销毁；重新启动时会设置新密码"}"
                  ${instance.status === "running" ? "" : "disabled"}
                ><span>${instance.status === "running" ? "查看凭证" : "凭证已销毁"}</span></button>`
              : ""}
            ${remoteUrl
              ? `<button class="row-button" type="button" data-action="copy" data-text="${escapeHtml(remoteUrl)}" aria-label="复制 ${escapeHtml(instance.name || "浏览器")} 的远程链接">
                  ${icon("copy")}<span>复制链接</span>
                </button>
                <button class="row-button primary-row" type="button" data-action="open-remote-browser" data-instance="${escapeHtml(instance.id)}" aria-label="远程打开 ${escapeHtml(instance.name || "浏览器")}">
                  ${icon("link")}<span>远程打开</span>
                </button>`
              : ""}
            <button class="row-button ${running ? "danger" : ""}" type="button" data-action="${action}" data-node="${escapeHtml(nodeId)}" data-instance="${escapeHtml(instance.id)}" aria-label="${actionLabel} ${escapeHtml(instance.name || "浏览器")}">
              ${icon(actionIcon)}<span>${actionLabel}</span>
            </button>
            <button class="row-button danger" type="button" data-action="delete-browser-instance" data-instance="${escapeHtml(instance.id)}" aria-label="永久删除 ${escapeHtml(instance.name || "浏览器")} 及其 Profile">
              ${icon("trash")}<span>删除</span>
            </button>
          </div>
        </article>
      `;
    })
    .join("");

  if (activeId) {
    const input = body.querySelector(
      `.browser-label-input[data-instance="${browserUi.cssEscape(activeId, window.CSS)}"]`
    );
    if (input) {
      input.focus();
      try {
        if (selStart != null) {
          input.setSelectionRange(selStart, selEnd != null ? selEnd : selStart);
        }
      } catch (_) {
        /* ignore */
      }
    }
  }
}

async function saveBrowserLabel(instanceId) {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  if (!instanceId) return;
  const input = document.querySelector(
    `.browser-label-input[data-instance="${browserUi.cssEscape(instanceId, window.CSS)}"]`
  );
  const label = String(input?.value || "").trim();
  await api("/api/browser/update", { instance_id: instanceId, label });
  await loadStatus({ forceFresh: true });
  if (appState.browserRecordNodeId) {
    renderBrowserRecords(appState.browserRecordNodeId);
  }
  showToast(label ? `标签已保存：${label}` : "标签已清空");
}

async function deleteBrowserInstance(instanceId) {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  const instance = browserInstanceById(instanceId);
  if (!instance) throw new Error("找不到浏览器记录");
  const syncEnabled = Boolean(appState.sync?.enabled && appState.sync?.url);
  const syncNotice = syncEnabled
    ? " WebDAV 已启用，远端记录和对应 Profile 包也会自动同步删除。"
    : "";
  if (!window.confirm(
    `删除「${instance.name || "浏览器"}」会先停止对应浏览器/Worker，并永久删除该记录的 Profile、Cookie 和本地存储；此操作无法恢复。${syncNotice}确定继续吗？`
  )) return null;
  const result = await api("/api/browser/delete", { instance_id: instanceId });
  await loadStatus({ forceFresh: true });
  if (appState.browserRecordNodeId) renderBrowserRecords(appState.browserRecordNodeId);
  showToast(syncEnabled ? "浏览器记录与本地数据已删除，正在同步清理 WebDAV" : "浏览器记录与本地数据已永久删除");
  return result;
}

function showBrowserRecords(nodeId, trigger = null) {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  if (!nodeId) return;
  appState.browserRecordNodeId = nodeId;
  lastBrowserRecordTrigger = trigger;
  lastBrowserRecordTriggerKey = trigger ? actionLockKey(trigger) : null;
  const modal = $("#browserRecordModal");
  openModal(modal);
  renderBrowserRecords(nodeId);
  modal.querySelector('[data-action="close-browser-records"]')?.focus();
}

/** Open browser records after an optional WebDAV pull so profiles are up to date. */
async function openBrowserRecordsWithSync(nodeId, trigger = null) {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  if (!nodeId) return;
  const sync = appState.sync || {};
  if (sync.enabled && sync.url) {
    try {
      showToast("正在从远端同步浏览器数据…");
      const result = await withSyncOperation(() =>
        api("/api/sync/pull", { preserve_local_changes: true })
      );
      if (result.settings) appState.sync = result.settings;
      await loadStatus({ forceFresh: true });
      if (result.skipped && result.reason === "local_changes_pending") {
        showToast("检测到本地未上传变更，已保留本地记录；请等待自动上传，或先上传到远端");
      } else if (result.skipped && result.reason === "already_latest") {
        showToast("已是最新");
      } else if (result.ok === false) {
        showToast((result.errors || []).join("; ") || "同步有错误，显示本地记录", "error");
      } else {
        showToast("远端同步完成");
      }
    } catch (error) {
      showToast(`同步失败，显示本地记录：${error.message}`, "error");
      try {
        await loadStatus({ forceFresh: true });
      } catch (_) {
        /* ignore */
      }
    }
  }
  showBrowserRecords(nodeId, trigger);
}

async function copyText(text) {
  let copied = false;
  try {
    const result = await api("/api/clipboard", { text });
    copied = result.ok === true;
  } catch {}

  if (!copied) {
    copied = await browserUi.copyText(text, window);
  }

  if (!copied) throw new Error("当前环境无法复制到剪贴板");

  showToast(`已复制 ${text}`);
}

async function startNode(nodeId) {
  const bridge = await api("/api/bridges/start", { node_id: nodeId, core: "auto" });
  await loadStatus({ forceFresh: true });
  await copyText(bridge.socks_url);
}

async function testNode(nodeId) {
  const result = await api("/api/nodes/test", { node_id: nodeId, core: "auto" });
  await loadStatus({ forceFresh: true });
  if (result.ok) {
    showToast(`通信正常：${result.latency_ms} ms`);
  } else {
    showToast(result.message || "测试失败", "error");
  }
  return result;
}

async function saveNodeCoreSettings(nodeId) {
  const select = document.querySelector(`select[data-core-select="${CSS.escape(nodeId)}"]`);
  const fallback = document.querySelector(`input[data-core-fallback="${CSS.escape(nodeId)}"]`);
  if (!select || !fallback) return;
  select.disabled = true;
  fallback.disabled = true;
  try {
    const result = await api("/api/nodes/core", {
      node_id: nodeId,
      preferred_core: select.value,
      fallback_enabled: fallback.checked,
    });
    const index = appState.nodes.findIndex((node) => node.id === nodeId);
    if (index >= 0) appState.nodes[index] = result.node;
    renderNodes();
    showToast(fallback.checked ? "核心偏好已保存，启动失败将尝试备用核心" : "核心偏好已保存");
  } catch (error) {
    await loadStatus({ forceFresh: true }).catch(() => {});
    throw error;
  }
}

async function requestBrowserLaunch(nodeId, instanceId = "", credentials = null) {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  const payload = instanceId ? { node_id: nodeId, instance_id: instanceId } : { node_id: nodeId };
  if (credentials) {
    payload.username = credentials.username;
    payload.password = credentials.password;
  }
  return api("/api/browser/launch", payload);
}

async function launchBrowser(nodeId, instanceId = "", trigger = null) {
  if (remoteBrowserEnabled()) {
    showRemoteBrowserLaunchModal(nodeId, instanceId, trigger);
    return null;
  }
  const result = await requestBrowserLaunch(nodeId, instanceId);
  await loadStatus({ forceFresh: true });
  showToast(`已启动${result.browser_name ? ` ${result.browser_name}` : "指纹浏览器"}：${result.proxy}`);
  return result;
}

function openRemoteBrowserLoadingWindow() {
  const remoteWindow = window.open("about:blank", "_blank");
  if (!remoteWindow) return null;
  try {
    remoteWindow.opener = null;
    remoteWindow.document.documentElement.lang = "zh-CN";
    remoteWindow.document.title = "正在启动远程浏览器";
    remoteWindow.document.body.style.cssText = "margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f5f7;color:#3a3a3c;font:14px -apple-system,BlinkMacSystemFont,sans-serif";
    remoteWindow.document.body.textContent = appState.remoteBrowser?.worker_image_ready === false
      ? "首次启动正在拉取远程浏览器 Worker 镜像；镜像较大，请稍候…"
      : "正在创建远程浏览器 Worker，请稍候…";
  } catch (_) {
    // about:blank 通常同源；若浏览器限制写入，仍可在启动成功后导航。
  }
  return remoteWindow;
}

async function handleRemoteBrowserLaunchSubmit(event) {
  event.preventDefault();
  if (remoteBrowserLaunchPromise || !pendingRemoteBrowserLaunch) return;

  const username = String($("#remoteBrowserUsername").value || "").trim();
  const password = String($("#remoteBrowserPassword").value || "");
  const passwordConfirm = String($("#remoteBrowserPasswordConfirm").value || "");
  const passwordLength = Array.from(password).length;
  if (!/^[A-Za-z0-9._-]{1,32}$/.test(username)) {
    showRemoteBrowserLaunchError("用户名必须为 1–32 位字母、数字、点、下划线或短横线");
    $("#remoteBrowserUsername").focus();
    return;
  }
  if (passwordLength < 10 || passwordLength > 128 || /[\x00-\x1f\x7f]/.test(password)) {
    showRemoteBrowserLaunchError("密码必须为 10–128 个字符，且不能包含控制字符");
    $("#remoteBrowserPassword").focus();
    return;
  }
  if (password !== passwordConfirm) {
    showRemoteBrowserLaunchError("两次输入的密码不一致");
    $("#remoteBrowserPasswordConfirm").focus();
    return;
  }
  if (appState.remoteBrowser?.available !== true) {
    const brokerMessage = appState.remoteBrowser?.error || "浏览器 Broker 尚未就绪，请检查 Docker 服务状态";
    showRemoteBrowserLaunchError(`无法启动远程浏览器：${brokerMessage}`);
    showToast(`浏览器 Broker 不可用：${brokerMessage}`, "error");
    return;
  }

  const pending = { ...pendingRemoteBrowserLaunch };
  const remoteWindow = openRemoteBrowserLoadingWindow();
  showRemoteBrowserLaunchError();
  setRemoteBrowserLaunchBusy(true);
  let result = null;
  let openedAutomatically = false;
  remoteBrowserLaunchPromise = requestBrowserLaunch(
    pending.nodeId,
    pending.instanceId,
    { username, password }
  );
  try {
    result = await remoteBrowserLaunchPromise;
    const remoteUrl = safeRemoteBrowserUrl(result.remote_url);
    if (!remoteUrl) throw new Error("浏览器 Worker 未返回有效的远程访问地址");
    showRemoteBrowserLaunchSuccess(result, { username, password });
    pendingRemoteBrowserLaunch = null;
    if (remoteWindow && !remoteWindow.closed) {
      try {
        remoteWindow.location.replace(remoteUrl);
        openedAutomatically = true;
      } catch (_) {
        // Worker 已成功启动；保留凭据成功页，让用户通过真实点击重新打开。
        try { remoteWindow.close(); } catch (_) {}
      }
    }
    try {
      await loadStatus({ forceFresh: true });
    } catch (statusError) {
      showToast(`远程浏览器已启动，但状态刷新失败：${statusError.message}`, "error");
    }
  } catch (error) {
    result = null;
    try { remoteWindow?.close(); } catch (_) {}
    try {
      await loadStatus({ forceFresh: true });
    } catch (_) {
      // 启动接口错误本身更具体，保留为主要提示。
    }
    const message = `远程浏览器启动失败：${error.message}`;
    showRemoteBrowserLaunchError(message);
    showToast(message, "error");
  } finally {
    remoteBrowserLaunchPromise = null;
    setRemoteBrowserLaunchBusy(false);
  }

  if (result) {
    const browserName = result.browser_name || "远程浏览器";
    const popupHint = openedAutomatically
      ? "已在新标签页打开；凭据会保留到你明确完成"
      : "请保存凭据并点击“打开远程浏览器”";
    showToast(`${browserName} 已启动，${popupHint}`);
    window.requestAnimationFrame(() => {
      if (!$("#remoteBrowserLaunchSuccess").hidden) {
        $("#remoteBrowserSuccessOpen")?.focus();
      }
    });
  }
}

// 停止某节点的指纹浏览器进程,保留其本地代理。
async function stopBrowser(nodeId, instanceId = "") {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  const payload = instanceId ? { instance_id: instanceId } : { node_id: nodeId };
  const result = await api("/api/browser/stop", payload);
  await loadStatus({ forceFresh: true });
  showToast(remoteBrowserEnabled() ? "已停止远程浏览器 Worker，代理端口保持运行" : "已停止浏览器，SOCKS5 端口保持运行");
  return result;
}

// 一键下载并安装指纹内核。下载体积较大(约 150-250MB),提示用户耐心等待。
function downloadKernel() {
  if (!browserEnabled()) return Promise.reject(new Error("当前运行模式不提供浏览器功能"));
  if (kernelDownloadPromise) return kernelDownloadPromise;

  showToast("正在下载指纹内核，约 150-250MB，请耐心等待…");
  kernelDownloadPromise = (async () => {
    const result = await api("/api/browser/download-kernel", {});
    await loadStatus({ forceFresh: true });
    if (result.already_installed) {
      showToast("指纹内核已安装");
    } else if (result.arch_warning) {
      // 架构不匹配(如 arm64 内核装到 Intel 机)时给出显著提示。
      showToast(result.arch_warning, "error");
    } else {
      showToast(`指纹内核安装完成：${result.version || ""}`);
    }
    return result;
  })().finally(() => {
    kernelDownloadPromise = null;
    renderCores();
  });
  renderCores();
  return kernelDownloadPromise;
}

function actionLockKey(button) {
  const action = button?.dataset?.action || "action";
  switch (action) {
    case "start":
    case "stop":
    case "restart":
    case "stop-all":
      return "bridge-lifecycle";
    case "refresh-sub":
    case "delete-sub":
      return `subscription:${button.dataset.id || ""}`;
    case "refresh-all-subs":
    case "cancel-refresh-all":
      return "subscription-batch";
    case "save-smart-gateway":
    case "stop-smart-gateway":
      return "smart-gateway";
    case "launch-browser":
    case "stop-browser":
      return `browser-node:${button.dataset.node || ""}`;
    case "launch-browser-instance":
    case "stop-browser-instance":
    case "save-browser-label":
    case "open-remote-browser":
    case "view-remote-browser-credentials":
    case "delete-browser-instance":
      return `browser-instance:${button.dataset.instance || ""}`;
    case "test":
      return `node-test:${button.dataset.node || ""}`;
    case "browser-records":
      return `browser-records:${button.dataset.node || ""}`;
    case "copy":
      return `copy:${button.dataset.text || ""}`;
    case "show-core-log":
      return `core-log:${button.dataset.core || ""}`;
    case "toggle-node-group":
      return `node-group:${button.dataset.group || ""}`;
    default:
      return `${action}:${button.dataset.id || button.id || "global"}`;
  }
}

function actionUsesSpinner(action) {
  return ![
    "close-core-log",
    "show-core-upload",
    "close-core-upload",
    "close-browser-records",
    "close-remote-browser-launch",
    "generate-remote-browser-password",
    "copy-remote-browser-password",
    "copy-launched-remote-browser-password",
    "copy-launched-remote-browser-url",
    "copy-launched-remote-browser-credentials",
    "open-launched-remote-browser",
    "finish-remote-browser-launch",
    "open-remote-browser",
    "close-remote-browser-credentials",
    "copy-viewed-remote-browser-password",
    "copy-viewed-remote-browser-url",
    "copy-viewed-remote-browser-credentials",
    "open-viewed-remote-browser",
    "copy",
    "browser-records",
    "toggle-node-group",
    "save-browser-label",
  ].includes(action);
}

function applyActionInFlightState() {
  document.querySelectorAll("button[data-action]").forEach((button) => {
    const busy = actionInFlight.has(actionLockKey(button));
    if (busy) {
      if (!("actionPreviousDisabled" in button.dataset)) {
        button.dataset.actionPreviousDisabled = button.disabled ? "true" : "false";
      }
      button.disabled = true;
      if (actionUsesSpinner(button.dataset.action)) button.classList.add("is-busy");
    } else if ("actionPreviousDisabled" in button.dataset) {
      button.disabled = button.dataset.actionPreviousDisabled === "true";
      delete button.dataset.actionPreviousDisabled;
      button.classList.remove("is-busy");
    }
  });
  if (pendingModalFocusKey) {
    const trigger = findActionTrigger(pendingModalFocusKey);
    if (trigger && !trigger.disabled) {
      trigger.focus();
      pendingModalFocusKey = null;
    }
  }
}

async function handleAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const lockKey = actionLockKey(button);
  if (actionInFlight.has(lockKey)) return;
  actionInFlight.add(lockKey);
  applyActionInFlightState();
  try {
    if (action === "toggle-node-group") {
      const groupId = button.dataset.group;
      if (groupId) {
        setNodeGroupCollapsed(groupId, !isNodeGroupCollapsed(groupId));
        renderNodes();
      }
    } else if (action === "start") {
      await startNode(button.dataset.node);
    } else if (action === "test") {
      await testNode(button.dataset.node);
    } else if (action === "launch-browser") {
      await launchBrowser(button.dataset.node, "", button);
    } else if (action === "launch-browser-instance") {
      await launchBrowser(button.dataset.node, button.dataset.instance, button);
    } else if (action === "stop-browser") {
      await stopBrowser(button.dataset.node);
    } else if (action === "stop-browser-instance") {
      await stopBrowser(button.dataset.node, button.dataset.instance);
    } else if (action === "browser-records") {
      await openBrowserRecordsWithSync(button.dataset.node, button);
    } else if (action === "save-browser-label") {
      await saveBrowserLabel(button.dataset.instance);
    } else if (action === "open-remote-browser") {
      openRemoteBrowserInstance(button.dataset.instance);
    } else if (action === "view-remote-browser-credentials") {
      await showRemoteBrowserCredentials(button.dataset.instance, button);
    } else if (action === "delete-browser-instance") {
      await deleteBrowserInstance(button.dataset.instance);
    } else if (action === "download-kernel") {
      await downloadKernel();
    } else if (action === "show-core-upload") {
      showCoreUploadModal(button);
    } else if (action === "copy") {
      await copyText(button.dataset.text);
    } else if (action === "rotate-pool-token") {
      if (!window.confirm("轮换代理池密钥后，所有旧链接会立即失效。确定继续吗？")) return;
      await api("/api/pool/token/rotate", {});
      await loadStatus({ forceFresh: true });
      showToast("代理池密钥已轮换，旧链接立即失效");
    } else if (action === "exclude-pool-member") {
      const duration = Number(button.dataset.duration || 0);
      await api("/api/pool/exclude", { id: button.dataset.id, duration_seconds: duration });
      await loadStatus({ forceFresh: true });
      showToast(duration ? "节点已暂时排除 30 分钟" : "节点已恢复到代理池");
    } else if (action === "kick-client") {
      await api("/api/network/clients/kick", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast("客户端连接已断开");
    } else if (action === "stop") {
      const result = await api("/api/bridges/stop", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast(result.killed_browsers ? "已停止端口，关联浏览器已关闭" : "已停止端口");
    } else if (action === "stop-all") {
      const result = await api("/api/bridges/stop-all", {});
      await loadStatus({ forceFresh: true });
      showToast(result.killed_browsers ? "已停止全部代理，关联浏览器已关闭" : "已停止全部代理");
    } else if (action === "restart") {
      const stopped = await api("/api/bridges/stop", { id: button.dataset.id });
      await api("/api/bridges/start", { node_id: button.dataset.node, core: "auto" });
      await loadStatus({ forceFresh: true });
      showToast(stopped.killed_browsers ? "已重启服务，关联浏览器已关闭" : "已重启服务");
    } else if (action === "refresh-sub") {
      const result = await api("/api/subscriptions/refresh", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      const changes = result.changes || {};
      showToast(result.not_modified
        ? "订阅远端未变化"
        : `订阅已刷新：新增 ${Number(changes.added || 0)}、删除 ${Number(changes.removed || 0)}、变更 ${Number(changes.changed || 0)}`);
    } else if (action === "refresh-all-subs") {
      const ids = appState.subscriptions.filter((item) => item.url).map((item) => item.id);
      if (!ids.length) {
        showToast("没有可刷新的 URL 订阅", "error");
      } else {
        const task = await api("/api/subscriptions/refresh-batch/start", { subscription_ids: ids });
        appState.subscriptionRefresh.tasks = [...(appState.subscriptionRefresh.tasks || []), task];
        renderActiveView({ force: true });
        showToast(`已开始刷新 ${ids.length} 个订阅`);
      }
    } else if (action === "cancel-refresh-all") {
      await api("/api/subscriptions/refresh-batch/cancel", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast("正在取消批量刷新");
    } else if (action === "save-smart-gateway") {
      await api("/api/smart-gateway/settings", {
        enabled: true,
        strategy: $("#smartGatewayStrategy").value,
        port: Number($("#smartGatewayPort").value),
        region: $("#smartGatewayRegion").value,
        query: $("#smartGatewayQuery").value,
      });
      await loadStatus({ forceFresh: true });
      showToast("智能网关设置已保存");
    } else if (action === "stop-smart-gateway") {
      await api("/api/smart-gateway/stop", {});
      await loadStatus({ forceFresh: true });
      showToast("智能网关已停止");
    } else if (action === "delete-sub") {
      const subscription = appState.subscriptions.find((item) => item.id === button.dataset.id);
      const name = subscription?.name || "该订阅";
      const nodeCount = Number(subscription?.node_count || (subscription?.node_ids || []).length || 0);
      if (!window.confirm(`删除「${name}」将清理最多 ${nodeCount} 个未被其他订阅引用的节点及浏览器 Profile。确定继续吗？`)) return;
      await api("/api/subscriptions/delete", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast("订阅已删除");
    } else if (action === "clear-logs") {
      await api("/api/logs/clear", {});
      await loadStatus({ forceFresh: true });
      appState.requestLogPage.page = 1;
      await loadLogPage("request");
      showToast("连接日志已清空");
    } else if (action === "clear-system-logs") {
      await api("/api/system/logs/clear", {});
      await loadStatus({ forceFresh: true });
      appState.systemLogPage.page = 1;
      await loadLogPage("system");
      showToast("系统日志已清空");
    } else if (action === "export-logs") {
      await downloadLogExport(button.dataset.kind || "request", button.dataset.format || "json");
      showToast("日志导出已开始下载");
    } else if (action === "reset-log-filters") {
      appState.logFilters.search = "";
      appState.logFilters.node = "all";
      appState.logFilters.status = "all";
      renderLogFilters();
      appState.requestLogPage.page = 1;
      await loadLogPage("request");
      showToast("日志筛选已重置");
    } else if (action === "show-core-log") {
      lastCoreLogTrigger = button;
      lastCoreLogTriggerKey = lockKey;
      await showCoreLog(button.dataset.core);
    } else if (action === "close-core-log") {
      closeCoreLogModal();
    } else if (action === "close-core-upload") {
      closeCoreUploadModal();
    } else if (action === "close-browser-records") {
      closeBrowserRecordModal();
    } else if (action === "close-remote-browser-launch") {
      closeRemoteBrowserLaunchModal();
    } else if (action === "generate-remote-browser-password") {
      fillGeneratedRemoteBrowserPassword({ announce: true });
    } else if (action === "copy-remote-browser-password") {
      await copyRemoteBrowserPassword();
    } else if (action === "copy-launched-remote-browser-password") {
      await copyLaunchedRemoteBrowserPassword();
    } else if (action === "copy-launched-remote-browser-url") {
      await copyLaunchedRemoteBrowserUrl();
    } else if (action === "copy-launched-remote-browser-credentials") {
      await copyLaunchedRemoteBrowserCredentials();
    } else if (action === "open-launched-remote-browser") {
      openLaunchedRemoteBrowser();
    } else if (action === "finish-remote-browser-launch") {
      finishRemoteBrowserLaunch();
    } else if (action === "close-remote-browser-credentials") {
      closeRemoteBrowserCredentialsModal();
    } else if (action === "copy-viewed-remote-browser-password") {
      await copyViewedRemoteBrowserPassword();
    } else if (action === "copy-viewed-remote-browser-url") {
      await copyViewedRemoteBrowserUrl();
    } else if (action === "copy-viewed-remote-browser-credentials") {
      await copyViewedRemoteBrowserCredentials();
    } else if (action === "open-viewed-remote-browser") {
      openViewedRemoteBrowser();
    } else if (action === "clear-selections") {
      // 清除所有选择
      clearNodeSelections();
    } else if (action === "batch-start-nodes") {
      // 批量启动选中的节点
      await batchStartNodes();
    }
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    actionInFlight.delete(lockKey);
    applyActionInFlightState();
    if (action === "stop-all" && appState.activeView === "bridges") {
      renderActiveView({ force: true });
    }
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
    form.querySelector(".raw-import-details")?.removeAttribute("open");
    await loadStatus({ forceFresh: true });
    showToast(`导入 ${result.subscription.node_count} 个节点`);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function saveSubscriptionSettings(control) {
  const previous = Boolean(appState.subscriptionSettings?.allow_non_public_urls);
  control.disabled = true;
  try {
    const result = await api("/api/settings/subscriptions", {
      allow_non_public_urls: Boolean(control.checked),
    });
    appState.subscriptionSettings = result.settings || {
      allow_non_public_urls: Boolean(control.checked),
    };
    renderActiveView({ force: true });
    showToast(control.checked ? "已允许非公网订阅地址" : "已恢复仅允许公网订阅地址");
  } catch (error) {
    control.checked = previous;
    showToast(error.message, "error");
  } finally {
    control.disabled = false;
  }
}

function trapModalFocus(event) {
  if (event.key !== "Tab") return false;
  const modal = !$("#remoteBrowserCredentialsModal").hidden
    ? $("#remoteBrowserCredentialsModal")
    : !$("#remoteBrowserLaunchModal").hidden
      ? $("#remoteBrowserLaunchModal")
      : !$("#coreUploadModal").hidden
        ? $("#coreUploadModal")
        : !$("#browserRecordModal").hidden
          ? $("#browserRecordModal")
          : !$("#coreLogModal").hidden
            ? $("#coreLogModal")
            : null;
  if (!modal) return false;
  const dialog = modal.querySelector('[role="dialog"]');
  const focusable = [...dialog.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
  )].filter((element) => !element.hidden && element.getClientRects().length > 0);
  if (!focusable.length) {
    event.preventDefault();
    dialog.focus();
    return true;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}

function scheduleStatusPoll(delay = document.hidden ? STATUS_POLL_HIDDEN_MS : STATUS_POLL_VISIBLE_MS) {
  window.clearTimeout(statusPollTimer);
  statusPollTimer = window.setTimeout(runStatusPoll, delay);
}

async function runStatusPoll({ reportError = false } = {}) {
  window.clearTimeout(statusPollTimer);
  statusPollTimer = null;
  try {
    await loadStatus();
  } catch (error) {
    if (reportError) showToast(error.message, "error");
  } finally {
    scheduleStatusPoll();
  }
}

function bindEvents() {
  // 侧边栏导航:点击切换视图。
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => setActiveView(item.dataset.view));
  });
  $("#importForm").addEventListener("submit", handleImport);
  $("#coreUploadForm")?.addEventListener("submit", handleCoreUploadSubmit);
  $("#coreUploadKind")?.addEventListener("change", () => {
    showCoreUploadError();
    updateCoreUploadControls({ clearFile: true });
  });
  $("#remoteBrowserLaunchForm")?.addEventListener("submit", handleRemoteBrowserLaunchSubmit);
  const networkForm = $("#networkSettingsForm");
  if (networkForm) {
    networkForm.addEventListener("input", markNetworkFormDirty);
    networkForm.addEventListener("change", markNetworkFormDirty);
    $("#lanAuthMode")?.addEventListener("change", syncLanAuthFields);
    networkForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = $("#networkSaveButton");
      try {
        await withNetworkSettingsSave(() => saveNetworkSettings(event), submit);
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  }
  const speedTestForm = $("#speedTestSettingsForm");
  if (speedTestForm) {
    speedTestForm.addEventListener("input", () => { speedTestFormDirty = true; });
    speedTestForm.addEventListener("change", () => { speedTestFormDirty = true; });
    speedTestForm.addEventListener("submit", async (event) => {
      const submit = $("#speedTestSaveButton");
      submit.disabled = true;
      submit.classList.add("is-busy");
      try {
        await saveSpeedTestSettings(event);
      } catch (error) {
        showToast(error.message, "error");
      } finally {
        submit.disabled = false;
        submit.classList.remove("is-busy");
      }
    });
  }
  const logSettingsForm = $("#logSettingsForm");
  if (logSettingsForm) {
    logSettingsForm.addEventListener("input", () => { logSettingsDirty = true; });
    logSettingsForm.addEventListener("change", () => { logSettingsDirty = true; });
    logSettingsForm.addEventListener("submit", async (event) => {
      const submit = $("#logSettingsSaveButton");
      submit.disabled = true;
      submit.classList.add("is-busy");
      try {
        await saveLogSettings(event);
      } catch (error) {
        showToast(error.message, "error");
      } finally {
        submit.disabled = false;
        submit.classList.remove("is-busy");
      }
    });
  }
  $("#diagnosticsNetworkEnabled")?.addEventListener("change", renderDiagnosticBridgeOptions);
  $("#diagnosticsRunButton")?.addEventListener("click", async () => {
    try {
      await runDiagnostics();
    } catch (error) {
      showToast(error.message, "error");
    }
  });
  $("#diagnosticsWebrtcButton")?.addEventListener("click", async () => {
    try {
      await runDiagnostics({ webrtcOnly: true });
    } catch (error) {
      showToast(error.message, "error");
    }
  });
  document.addEventListener("change", async (event) => {
    if (event.target.matches("#allowNonPublicSubscriptions")) {
      await saveSubscriptionSettings(event.target);
      return;
    }
    const coreControl = event.target.closest("[data-core-select], [data-core-fallback]");
    if (coreControl) {
      const nodeId = coreControl.dataset.coreSelect || coreControl.dataset.coreFallback;
      try {
        await saveNodeCoreSettings(nodeId);
      } catch (error) {
        showToast(error.message, "error");
      }
      return;
    }
    const select = event.target.closest('select[data-action="schedule-sub"]');
    if (!select) return;
    select.disabled = true;
    try {
      await api("/api/subscriptions/schedule", {
        id: select.dataset.id,
        refresh_interval_minutes: Number(select.value),
      });
      await loadStatus({ forceFresh: true });
      showToast(Number(select.value) ? "订阅自动刷新周期已更新" : "订阅自动刷新已关闭");
    } catch (error) {
      showToast(error.message, "error");
      await loadStatus({ forceFresh: true }).catch(() => {});
    } finally {
      select.disabled = false;
    }
  });
  const syncForm = $("#syncForm");
  if (syncForm) {
    // 任意输入/勾选都标记脏数据，避免 3.5s 状态轮询把未保存内容冲掉。
    syncForm.addEventListener("input", markSyncFormDirty);
    syncForm.addEventListener("change", markSyncFormDirty);
    syncForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = syncForm.querySelector('button[type="submit"]');
      try {
        await withSyncOperation(() => saveSyncSettings(event), submit);
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  }
  const bindSyncBtn = (id, kind) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener("click", async () => {
      try {
        await withSyncOperation(() => runSyncAction(kind), btn);
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  };
  bindSyncBtn("syncTestBtn", "test");
  bindSyncBtn("syncPushBtn", "push");
  bindSyncBtn("syncPullBtn", "pull");
  bindSyncBtn("syncCheckBtn", "check");
  document.addEventListener("click", handleAction);
  $("#refreshStatus").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.classList.add("is-busy");
    button.disabled = true;
    try {
      await loadStatus({ forceFresh: true });
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
      await loadStatus({ forceFresh: true });
      showToast("列表已刷新");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      button.classList.remove("is-busy");
      button.disabled = false;
    }
  });
  $("#testVisible").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const nodes = filteredNodes();
    if (!nodes.length) {
      showToast("没有可测试的节点", "error");
      return;
    }
    if (nodes.length > 500) {
      showToast(`当前筛选有 ${nodes.length} 个节点，批量测速上限为 500，请缩小筛选范围`, "error");
      return;
    }
    button.disabled = true;
    try {
      const task = await api("/api/nodes/test-batch/start", { node_ids: nodes.map((node) => node.id) });
      appState.speedTest.tasks = [...(appState.speedTest.tasks || []), task];
      renderSpeedTestTaskControls();
      showToast(`已启动 ${nodes.length} 个节点的批量测速`);
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      renderSpeedTestTaskControls();
    }
  });
  $("#cancelVisibleTest").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const taskId = button.dataset.taskId;
    if (!taskId) return;
    button.disabled = true;
    try {
      await api("/api/nodes/test-batch/cancel", { id: taskId });
      await loadStatus({ forceFresh: true });
      showToast("正在取消批量测速");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      renderSpeedTestTaskControls();
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
  $("#nodeSort").addEventListener("change", (event) => {
    appState.filters.sort = event.target.value;
    renderNodes();
  });
  const subscriptionFilter = $("#subscriptionFilter");
  if (subscriptionFilter) {
    subscriptionFilter.addEventListener("change", (event) => {
      appState.filters.subscription = event.target.value;
      renderNodes();
    });
  }
  $("#searchInput").addEventListener("input", (event) => {
    appState.filters.search = event.target.value;
    renderNodes();
  });
  $("#logSearchInput").addEventListener("input", (event) => {
    appState.logFilters.search = event.target.value;
    scheduleLogPageLoad("request");
  });
  $("#logNodeFilter").addEventListener("change", (event) => {
    appState.logFilters.node = event.target.value;
    appState.requestLogPage.page = 1;
    loadLogPage("request").catch((error) => showToast(error.message, "error"));
  });
  $("#logStatusFilter").addEventListener("change", (event) => {
    appState.logFilters.status = event.target.value;
    appState.requestLogPage.page = 1;
    loadLogPage("request").catch((error) => showToast(error.message, "error"));
  });
  $("#systemSearchInput").addEventListener("input", (event) => {
    appState.systemLogFilters.search = event.target.value;
    scheduleLogPageLoad("system");
  });
  $("#systemLevelFilter").addEventListener("change", (event) => {
    appState.systemLogFilters.level = event.target.value;
    appState.systemLogPage.page = 1;
    loadLogPage("system").catch((error) => showToast(error.message, "error"));
  });
  $("#requestLogPrev").addEventListener("click", () => {
    appState.requestLogPage.page = Math.max(1, appState.requestLogPage.page - 1);
    loadLogPage("request").catch((error) => showToast(error.message, "error"));
  });
  $("#requestLogNext").addEventListener("click", () => {
    appState.requestLogPage.page = Math.min(appState.requestLogPage.page_count, appState.requestLogPage.page + 1);
    loadLogPage("request").catch((error) => showToast(error.message, "error"));
  });
  $("#systemLogPrev").addEventListener("click", () => {
    appState.systemLogPage.page = Math.max(1, appState.systemLogPage.page - 1);
    loadLogPage("system").catch((error) => showToast(error.message, "error"));
  });
  $("#systemLogNext").addEventListener("click", () => {
    appState.systemLogPage.page = Math.min(appState.systemLogPage.page_count, appState.systemLogPage.page + 1);
    loadLogPage("system").catch((error) => showToast(error.message, "error"));
  });
  $("#coreLogModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeCoreLogModal();
    }
  });
  $("#coreUploadModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeCoreUploadModal();
    }
  });
  $("#browserRecordModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeBrowserRecordModal();
    }
  });
  $("#remoteBrowserLaunchModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeRemoteBrowserLaunchModal();
    }
  });
  $("#remoteBrowserCredentialsModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeRemoteBrowserCredentialsModal();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (trapModalFocus(event)) return;
    if (event.key === "Escape" && !$("#remoteBrowserCredentialsModal").hidden) {
      closeRemoteBrowserCredentialsModal();
    } else if (event.key === "Escape" && !$("#remoteBrowserLaunchModal").hidden) {
      closeRemoteBrowserLaunchModal();
    } else if (event.key === "Escape" && !$("#coreUploadModal").hidden) {
      closeCoreUploadModal();
    } else if (event.key === "Escape" && !$("#coreLogModal").hidden) {
      closeCoreLogModal();
    } else if (event.key === "Escape" && !$("#browserRecordModal").hidden) {
      closeBrowserRecordModal();
    }
  });
  document.addEventListener("visibilitychange", () => {
    scheduleStatusPoll(document.hidden ? STATUS_POLL_HIDDEN_MS : 0);
  });
  window.addEventListener("beforeunload", () => {
    window.clearTimeout(statusPollTimer);
    window.clearTimeout(logFilterTimer);
    statusAbortController?.abort();
  });
}

bindEvents();
// 初始视图:优先使用上次记忆的选择(在 <html> 上的 data-initial-view)。
setActiveView(document.documentElement.getAttribute("data-initial-view") || "nodes");
runStatusPoll({ reportError: true });
