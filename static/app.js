const appState = {
  version: "",
  cores: {},
  coreCatalog: [],
  features: { browser: true, browser_profile_sync: true, remote_browser: false, docker: false },
  runtime: { mode: "headless", docker: false },
  remoteBrowser: { configured: false, available: false, active_sessions: 0, error: "" },
  platform: null,
  fingerprint_chromium: null,
  // 多版本指纹内核、扩展库与检测站点，均来自 /api/status。
  fingerprint_kernels: [],
  fingerprint_default_kernel: "",
  browser_extensions: [],
  fingerprint_check_sites: {},
  active_browsers: [],
  browser_instances: [],
  subscriptions: [],
  nodes: [],
  regions: [],
  bridges: [],
  tests: {},
  nodeHistories: {},
  healthSettings: { auto_switch: false },
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
  activeView: "overview",
  filters: {
    protocol: "all",
    region: "all",
    subscription: "all",
    sort: "name",
    search: "",
    status: "all",
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
  // 指纹浏览器页的多选记录 ID
  selectedBrowserIds: new Set(),
};

const managementAuth = window.ProxyBridgeAuth;
const browserUi = window.ProxyBridgeBrowserUi;
const browserConfig = window.ProxyBridgeBrowserConfig;
const coreUploadClient = window.ProxyBridgeCoreUpload;
const uiCore = window.ProxyBridgeUiCore;
const i18n = window.ProxyBridgeI18n;
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
let pendingRemoteBrowserBatchNodeIds = [];
// 浏览器页多选启动远程浏览器时的目标记录：[{ nodeId, instanceId }]
let pendingRemoteBrowserBatchInstances = [];
let browserConfigState = null;
let lastBrowserConfigTrigger = null;
let confirmOptionChecked = false;
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
let statusPollDeadline = 0;
let syncOperationPromise = null;
let networkSettingsSavePromise = null;
let logPageRequestGeneration = 0;
let logFilterTimer = null;
let logSettingsHydrated = false;
let logSettingsDirty = false;
const actionInFlight = new Set();
const renderSignatures = new Map();
let confirmResolver = null;
let currentNodeDetailId = null;
let currentNodeDetailTab = "overview";
let statusEventController = null;
let statusEventConnected = false;
let nodeVirtualFrame = 0;
const revealedSecrets = new Set();
let browserPageFilter = { node: "all", status: "all", search: "", group: "all" };

const renderOverview = window.ProxyBridgePages.createOverviewRenderer({ $, appState, runningBridges, escapeHtml, formatLogTime, icon });
const renderKernelPage = window.ProxyBridgePages.createKernelRenderer({ $, appState, browserEnabled, escapeHtml });
const renderBrowserPage = window.ProxyBridgePages.createBrowserRenderer({ $, appState, browserPageFilter, browserUi, browserConfig, browserEnabled, remoteBrowserEnabled, escapeHtml, nodeById, safeRemoteBrowserUrl, formatRelativeTime, browserStatusLabel, uiCore });
const renderNodes = window.ProxyBridgePages.createNodeRenderer({ $, appState, bridgeForNode, mostRecentBridgeForNode, browserEnabled, browserInstancesForNode, runningBrowserInstancesForNode, escapeHtml, icon, latencyCell, isNodeGroupCollapsed, groupAverageLatency, initials, filteredNodes, groupNodesBySubscription, renderSpeedTestTaskControls, updateBatchActionsBar, bindNodeCheckboxes, uiCore });
let currentPoolLinkFormat = "list_json";
let poolLinksExpanded = null;
let subscriptionPreviewSignature = "";
let subscriptionPreviewResult = null;

const STATUS_REQUEST_TIMEOUT_MS = 12_000;
const STATUS_POLL_VISIBLE_MS = 3_500;
const STATUS_POLL_HIDDEN_MS = 30_000;
// 延迟显示阈值：300ms 起提示偏慢，800ms 起提示慢，仅影响界面着色。
const LATENCY_WARNING_MS = 300;
const LATENCY_SLOW_MS = 800;

// 侧边栏可切换的视图列表,顺序与 HTML 中 .nav-item / .view 一致。
const VIEW_IDS = ["overview", "nodes", "subscriptions", "bridges", "pool", "browsers", "logs", "system", "settings", "cores", "sync", "diagnostics"];

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
    const selected = item.dataset.view === view || (item.dataset.view === "logs" && view === "system");
    item.setAttribute("aria-current", selected ? "true" : "false");
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });
  document.querySelectorAll("[data-log-tab]").forEach((tab) => {
    const active = tab.dataset.logTab === view;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  renderActiveView({ force: true });
  i18n.apply(document);
  applyActionInFlightState();
  if (view === "logs") {
    loadLogPage("request").catch((error) => showToast(error.message, "error"));
  } else if (view === "system") {
    loadLogPage("system").catch((error) => showToast(error.message, "error"));
  } else if (view === "cores") {
    loadCoreCatalog().catch((error) => showToast(error.message, "error"));
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
  const errorCount = Number(appState.logCenter?.system_error_total ?? (appState.systemLogs || []).filter((entry) => /error|critical|错误|致命/i.test(String(entry.level || entry.severity || ""))).length);
  setNavCount("navCountLogs", errorCount);
  const errorBadge = document.getElementById("systemErrorCount");
  for (const badge of [errorBadge, document.getElementById("systemErrorCountSystem")]) {
    if (badge) {
      badge.hidden = errorCount === 0;
      badge.textContent = String(errorCount);
    }
  }
}

// option 为可选复选框 { label, checked }；用户确认后通过 confirmOptionChecked 读取勾选结果。
function confirmAction(message, { title = "确认操作", confirmLabel = "继续", details = "", option = null } = {}) {
  const dialog = $("#confirmDialog");
  if (!dialog) return Promise.reject(new Error("无法打开操作确认对话框，请刷新页面后重试"));
  $("#confirmTitle").textContent = title;
  $("#confirmMessage").textContent = message;
  const detailEl = $("#confirmDetails");
  detailEl.textContent = details;
  detailEl.hidden = !details;
  const optionEl = $("#confirmOption");
  confirmOptionChecked = false;
  if (optionEl) {
    optionEl.hidden = !option;
    $("#confirmOptionLabel").textContent = option?.label || "";
    $("#confirmOptionInput").checked = Boolean(option?.checked);
  }
  $("#confirmAccept").textContent = confirmLabel;
  dialog.hidden = false;
  dialog.querySelector("[role=alertdialog]").focus();
  return new Promise((resolve) => { confirmResolver = resolve; });
}

function finishConfirmation(accepted) {
  const dialog = $("#confirmDialog");
  if (!dialog || dialog.hidden) return;
  dialog.hidden = true;
  confirmOptionChecked = Boolean(accepted && !$("#confirmOption")?.hidden && $("#confirmOptionInput")?.checked);
  if (confirmResolver) confirmResolver(Boolean(accepted));
  confirmResolver = null;
}

function openDrawer(drawer, focusSelector = null) {
  if (!drawer) return;
  drawer.hidden = false;
  document.body.classList.add("has-open-drawer");
  requestAnimationFrame(() => (focusSelector ? drawer.querySelector(focusSelector) : drawer.querySelector("[role=dialog]") || drawer).focus());
}

function closeDrawer(drawer) {
  if (!drawer) return;
  drawer.hidden = true;
  if (!document.querySelector(".drawer-backdrop:not([hidden])")) document.body.classList.remove("has-open-drawer");
}

function setNodeDensity(value) {
  const density = value === "standard" ? "standard" : "compact";
  document.documentElement.dataset.nodeDensity = density;
  try { localStorage.setItem("pb-node-density", density); } catch (_) {}
  for (const selector of ["#nodeDensity", "#settingsNodeDensity"]) {
    const control = $(selector);
    if (control && document.activeElement !== control) control.value = density;
  }
  if (appState.activeView === "nodes") renderNodes();
}

const COMMAND_PAGES = [
  ["overview", "概览"], ["nodes", "节点"], ["bridges", "运行中代理"], ["pool", "代理池与网关"],
  ["browsers", "指纹浏览器"], ["subscriptions", "订阅"], ["logs", "连接日志"],
  ["settings", "通用设置"], ["cores", "内核管理"], ["sync", "WebDAV 同步"], ["diagnostics", "诊断中心"],
];

function renderCommandResults() {
  const query = String($("#commandSearch")?.value || "").trim().toLowerCase();
  const pages = COMMAND_PAGES.filter(([, label]) => label.toLowerCase().includes(query));
  const nodes = appState.nodes.filter((node) => [node.name, node.server, node.region, node.region_code, node.remark].some((value) => String(value || "").toLowerCase().includes(query))).slice(0, 8);
  $("#commandResults").innerHTML = `<section class="command-group"><span>页面</span>${pages.map(([view, label]) => `<button type="button" data-action="command-run" data-command="page" data-view="${view}"><span>${escapeHtml(label)}</span><kbd>↵</kbd></button>`).join("")}</section>${nodes.length ? `<section class="command-group"><span>节点</span>${nodes.map((node) => `<button type="button" data-action="command-run" data-command="node" data-node="${escapeHtml(node.id)}"><span><strong>${escapeHtml(node.name)}</strong><small>${escapeHtml(node.region || node.region_code || node.server || "")}</small></span><kbd>启动 / 复制</kbd></button>`).join("")}</section>` : ""}`;
}

function openCommandPalette() {
  const palette = $("#commandPalette");
  palette.hidden = false;
  $("#commandSearch").value = "";
  renderCommandResults();
  requestAnimationFrame(() => $("#commandSearch").focus());
}

function closeCommandPalette() {
  $("#commandPalette").hidden = true;
}

function groupAverageLatency(nodes) {
  const values = nodes.map((node) => appState.tests[node.id]).filter((item) => item?.ok && Number.isFinite(Number(item.latency_ms))).map((item) => Number(item.latency_ms));
  return values.length ? `${Math.round(values.reduce((sum, item) => sum + item, 0) / values.length)} ms` : "—";
}


async function loadCoreCatalog() {
  const result = await api("/api/cores");
  appState.coreCatalog = Array.isArray(result.cores) ? result.cores : [];
  renderKernelPage();
}


function renderNodeDetailDrawer() {
  const node = nodeById(currentNodeDetailId);
  if (!node) return closeDrawer($("#nodeDetailDrawer"));
  const bridge = bridgeForNode(node.id);
  $("#nodeDetailTitle").textContent = node.name || "节点详情";
  $("#nodeDetailStatus").textContent = bridge ? `运行中 · :${bridge.port}` : "已停止";
  const content = $("#nodeDrawerContent");
  const tests = appState.tests[node.id];
  const coreOptions = Array.isArray(node.core_options) && node.core_options.length ? node.core_options : [node.preferred_core || "sing-box"];
  // 地区选项以服务端目录为准，避免界面提供后端不接受的代码；空值表示自动识别。
  const regionOptions = [...appState.regions, { code: "ZZ", name: "未分类" }];
  const history = Array.isArray(appState.nodeHistories[node.id]) ? appState.nodeHistories[node.id] : (Array.isArray(tests?.history) ? tests.history.slice().reverse() : (tests ? [tests] : []));
  if (currentNodeDetailTab === "overview") content.innerHTML = `<div class="detail-stat-grid"><div><span>地区</span><strong>${escapeHtml(node.region_code || node.region || "未分类")}</strong></div><div><span>协议</span><strong>${escapeHtml([node.protocol, node.transport, node.security].filter(Boolean).join(" · ") || "—")}</strong></div><div><span>延迟</span><strong>${escapeHtml(tests?.ok ? `${tests.latency_ms} ms` : tests ? "测速失败" : "未测试")}</strong></div></div><section class="drawer-section"><h3>服务器</h3><code>${escapeHtml(`${node.server}:${node.port}`)}</code></section><section class="drawer-section"><h3>备注</h3><p>${escapeHtml(node.remark || "暂无备注")}</p></section><section class="drawer-section"><h3>运行状态</h3><p>${bridge ? `本地端口 :${escapeHtml(bridge.port)} · SOCKS5 与 HTTP 可用` : "当前未运行"}</p><button class="row-button" data-action="${bridge ? "copy" : "start"}" ${bridge ? `data-text="${escapeHtml(bridge.socks_url)}"` : `data-node="${escapeHtml(node.id)}"`}>${bridge ? "复制 SOCKS5 地址" : "启动节点"}</button></section>`;
  else if (currentNodeDetailTab === "browsers") content.innerHTML = `<div class="drawer-browser-list">${browserInstancesForNode(node.id).length ? browserInstancesForNode(node.id).map((instance) => `<div class="drawer-browser-row"><div><strong>${escapeHtml(instance.label || instance.name || "浏览器")}</strong><span>${escapeHtml(browserStatusLabel(instance.status))} · ${escapeHtml(formatTime(browserUi.browserInstanceLastUsedAt(instance)))}</span></div><button class="row-button" data-action="open-browser-config" data-instance="${escapeHtml(instance.id)}">配置</button><button class="row-button" data-action="${["running", "starting", "stopping"].includes(instance.status) ? "stop-browser-instance" : "launch-browser-instance"}" data-instance="${escapeHtml(instance.id)}" data-node="${escapeHtml(node.id)}">${["running", "starting", "stopping"].includes(instance.status) ? "停止" : "启动"}</button><button class="icon-button danger" data-action="delete-browser-instance" data-instance="${escapeHtml(instance.id)}" aria-label="删除浏览器记录">×</button></div>`).join("") : `<div class="compact-empty">此节点还没有浏览器记录。</div>`}</div>${node.browser_preserved ? `<p class="text-warning">该节点已从订阅中移除，仅为浏览器记录保留。请在浏览器配置中换绑到当前订阅的节点。</p>` : `<button class="toolbar-button" data-action="open-browser-create" data-node="${escapeHtml(node.id)}">新建浏览器记录…</button>`}`;
  else if (currentNodeDetailTab === "history") content.innerHTML = history.length ? `<div class="test-history-list">${history.map((item) => `<div><span class="state-dot ${item.ok ? "is-ok" : "is-error"}"></span><strong>${item.ok ? `${escapeHtml(item.latency_ms)} ms` : "失败"}</strong><span>${escapeHtml(item.message || "测速完成")}</span><time>${escapeHtml(formatTime(item.tested_at || item.ts || item.at))}</time></div>`).join("")}</div><button class="toolbar-button" data-action="test" data-node="${escapeHtml(node.id)}">重新测速</button>` : `<div class="compact-empty">暂无测速历史。<button class="toolbar-button" data-action="test" data-node="${escapeHtml(node.id)}">立即测速</button></div>`;
  else content.innerHTML = `<form class="node-config-form" id="nodeDetailForm"><label><span>地区覆盖</span><select id="nodeDetailRegion"><option value="">自动识别</option>${regionOptions.map(({ code, name }) => `<option value="${escapeHtml(code)}" ${node.region_override === code ? "selected" : ""}>${escapeHtml(code)} · ${escapeHtml(name)}</option>`).join("")}</select></label><label><span>备注名</span><input id="nodeDetailRemark" maxlength="500" value="${escapeHtml(node.remark || "")}" placeholder="用于识别节点的自定义备注"></label><label><span>首选核心</span><select data-core-select="${escapeHtml(node.id)}" ${bridge ? "disabled" : ""}>${coreOptions.map((core) => `<option value="${escapeHtml(core)}" ${core === node.preferred_core ? "selected" : ""}>${escapeHtml(core)}</option>`).join("")}</select></label><label class="sync-checkbox"><input type="checkbox" data-core-fallback="${escapeHtml(node.id)}" ${node.fallback_enabled ? "checked" : ""} ${bridge ? "disabled" : ""}><span>首选核心失败后尝试备用核心</span></label><label class="sync-checkbox"><input id="nodeDetailFavorite" type="checkbox" ${node.favorite ? "checked" : ""}><span>收藏节点并置顶</span></label><button class="primary-button" type="button" data-action="save-node-details" data-node="${escapeHtml(node.id)}">保存详情</button></form><details class="technical-details"><summary>技术信息</summary><dl><dt>节点 ID</dt><dd><code>${escapeHtml(node.id)}</code></dd><dt>服务器</dt><dd><code>${escapeHtml(`${node.server}:${node.port}`)}</code></dd><dt>核心回退</dt><dd>${node.fallback_enabled ? "已启用" : "关闭"}</dd></dl></details>`;
  document.querySelectorAll("[data-node-tab]").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.nodeTab === currentNodeDetailTab);
    tab.setAttribute("aria-selected", tab.dataset.nodeTab === currentNodeDetailTab ? "true" : "false");
  });
}

function openNodeDetail(nodeId, tab = "overview") {
  if (!nodeById(nodeId)) return;
  currentNodeDetailId = nodeId;
  currentNodeDetailTab = tab;
  renderNodeDetailDrawer();
  openDrawer($("#nodeDetailDrawer"));
  if (tab === "history") loadNodeHistory(nodeId).catch((error) => showToast(error.message, "error"));
}

async function loadNodeHistory(nodeId) {
  const result = await api(`/api/nodes/history?node_id=${encodeURIComponent(nodeId)}`);
  appState.nodeHistories[nodeId] = Array.isArray(result.history) ? result.history : [];
  if (currentNodeDetailId === nodeId && currentNodeDetailTab === "history") renderNodeDetailDrawer();
  return appState.nodeHistories[nodeId];
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

function showToast(message, kind = "ok", { duration } = {}) {
  const toast = $("#toast");
  toast.replaceChildren(document.createTextNode(message));
  toast.classList.toggle("error", kind === "error");
  if (kind === "error") {
    const logLink = document.createElement("button");
    logLink.type = "button";
    logLink.className = "toast-log-link";
    logLink.textContent = "查看日志";
    logLink.addEventListener("click", () => setActiveView("system"), { once: true });
    toast.append(logLink);
  }
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  const timeout = duration ?? (kind === "error" ? 0 : 3000);
  if (timeout > 0) showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), timeout);
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

function formatRelativeTime(value) {
  if (!value) return "—";
  const timestamp = typeof value === "number" ? value : Date.parse(value) / 1000;
  if (!Number.isFinite(timestamp)) return "—";
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  if (delta < 60) return "刚刚";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 86400 * 30) return `${Math.floor(delta / 86400)} 天前`;
  return formatTime(timestamp);
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
  updateSettingsSaveBar();
}

function anyGeneralSettingsDirty() {
  return networkFormDirty || speedTestFormDirty || logSettingsDirty;
}

function updateSettingsSaveBar() {
  const bar = $("#settingsSaveBar");
  if (bar) bar.hidden = !anyGeneralSettingsDirty();
}

function applyNetworkSettingsToForm(network, { force = false } = {}) {
  if (networkFormDirty && !force) return;
  const allowLan = $("#allowLan");
  if (!allowLan) return;
  allowLan.checked = Boolean(network?.allow_lan);
  const security = network?.security || {};
  $("#lanAuthMode").value = security.auth_mode || "none";
  $("#lanAuthMode").dataset.previousAuthMode = security.auth_mode || "none";
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
  const lanChecked = Boolean($("#allowLan")?.checked);
  const securityFields = $(".network-security-grid");
  const warning = $("#networkShareWarning");
  if (securityFields) securityFields.hidden = !lanChecked;
  if (warning) warning.hidden = !lanChecked;
  const authMode = $("#lanAuthMode")?.value || "none";
  $("#lanUsernameField").hidden = !lanChecked || authMode !== "basic";
  $("#lanPasswordField").hidden = !lanChecked || authMode !== "basic";
  $("#lanTokenField").hidden = !lanChecked || authMode !== "token";
  const density = localStorage.getItem("pb-node-density") === "standard" ? "standard" : "compact";
  const densityControl = $("#settingsNodeDensity");
  if (densityControl && document.activeElement !== densityControl) densityControl.value = density;
  const version = $("#aboutVersion");
  if (version) version.textContent = appState.version ? `v${appState.version}` : "—";
  const themeSelect = $("#themeSelect");
  if (themeSelect && document.activeElement !== themeSelect) themeSelect.value = localStorage.getItem("pb-theme") || "system";
  const languageSelect = $("#languageSelect");
  if (languageSelect && document.activeElement !== languageSelect) languageSelect.value = i18n.getLocale();
  const healthToggle = $("#healthAutoSwitch");
  if (healthToggle && document.activeElement !== healthToggle) healthToggle.checked = appState.healthSettings?.auto_switch === true;

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
  updateSettingsSaveBar();
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
  updateSettingsSaveBar();
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
  updateSettingsSaveBar();
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
  if (result.generated_token) {
    $("#lanAuthMode").value = "token";
    $("#lanTokenField").hidden = false;
    $("#lanToken").value = result.generated_token;
  }
  showToast(
    `${reboundCount > 0 ? `网络设置已保存，已重新绑定 ${reboundCount} 个运行中代理，原有连接可能已中断。` : "网络设置已保存。"}${result.generated_token ? "新 Token 仅显示本次，请复制并妥善保存。" : ""}`,
    "ok",
    { duration: result.generated_token ? 9000 : undefined },
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
      ? "在多台设备之间同步<strong>订阅 / 节点</strong>与<strong>已停止的指纹浏览器 Profile</strong>（Cookie、LocalStorage 等），实现换机接力。删除订阅时默认保留关联浏览器数据，可在确认框中选择一并删除；上传前请先关闭目标浏览器。"
      : "当前为 Docker / 无浏览器模式，WebDAV 仅同步<strong>订阅与节点</strong>。上传不会打包、覆盖或删除远端已有的浏览器 Profile；拉取也会保留本地浏览器数据。";
  }
  const autoUploadOption = $("#syncAutoUploadOption");
  if (autoUploadOption) autoUploadOption.hidden = !browserSyncEnabled;
  const autoUpload = $("#syncAutoUpload");
  if (autoUpload) autoUpload.disabled = !browserSyncEnabled;
  const encryptPassword = $("#syncEncryptPasswordField");
  if (encryptPassword) encryptPassword.hidden = !$("#syncEncrypt")?.checked;
  const configured = Boolean(String(settings.url || "").trim());
  const enabled = Boolean(configured && settings.enabled);
  for (const [id, available] of [["syncTestBtn", configured], ["syncCheckBtn", configured], ["syncPushBtn", enabled], ["syncPullBtn", enabled]]) {
    const button = $(`#${id}`);
    if (button) {
      button.disabled = !available || Boolean(syncOperationPromise);
      button.title = available ? "" : configured ? "请先启用同步" : "请先配置 WebDAV URL";
    }
  }
  const card = $("#syncStatusCard");
  if (card) {
    const ready = enabled;
    card.innerHTML = `<div class="sync-status-main"><span class="state-dot ${ready ? "is-ok" : "is-idle"}"></span><div><strong>${ready ? "同步已启用" : configured ? "已配置 · 尚未启用" : "WebDAV 未配置"}</strong><span>${escapeHtml(settings.last_status || (ready ? "等待同步" : "填写服务器信息以开始同步"))}</span></div></div><div class="sync-status-meta"><span>上次上传 <strong>${escapeHtml(formatRelativeTime(settings.last_push_at))}</strong></span><span>上次拉取 <strong>${escapeHtml(formatRelativeTime(settings.last_pull_at))}</strong></span><span>远端更新 <strong>${escapeHtml(formatRelativeTime(settings.last_remote_updated_at))}</strong></span></div>${settings.last_error ? `<p class="sync-status-error">${escapeHtml(settings.last_error)}</p>` : ""}`;
  }
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
  let payload = {};
  if (kind === "pull") {
    const preview = await api("/api/sync/preview", {});
    if (!preview?.ok) throw new Error(preview?.error || "无法预览远端数据，请检查同步设置");
    if (preview.already_latest || preview.remote_newer === false) {
      showToast("远端没有比本地更新的数据，无需拉取");
      return preview;
    }
    const remote = preview.remote || {};
    const summary = preview.summary || {};
    const summaryLines = [];
    const describe = (label, item) => {
      if (!item || typeof item !== "object") return;
      const added = Number(item.added ?? item.added_count ?? 0);
      const removed = Number(item.removed ?? item.removed_count ?? 0);
      const updated = Number(item.updated ?? item.updated_count ?? 0);
      summaryLines.push(`${label}：新增 ${added} · 删除 ${removed} · 更新 ${updated}`);
    };
    describe("订阅", summary.subscriptions);
    describe("节点", summary.nodes);
    describe("Profile", summary.profiles || summary.browser_profiles);
    if (!summaryLines.length) summaryLines.push(`远端数据：${Number(summary.remote_profile_count || 0)} 个 Profile`);
    const remoteLabel = [remote.device_name || remote.device_id || "未知设备", formatRelativeTime(remote.updated_at || remote.timestamp)].filter(Boolean).join(" · ");
    if (!await confirmAction("拉取会覆盖本机同步范围内的订阅、节点和浏览器 Profile。请确认已关闭冲突的浏览器。", {
      title: "确认从远端拉取",
      confirmLabel: "拉取并覆盖",
      details: `远端：${remoteLabel}\n${summaryLines.join("\n")}\n\n将只应用本次预览的状态版本；若远端在此期间变化，需要重新预览。`,
    })) return;
    payload.expected_state_sha256 = preview.state_sha256;
  }
  const result = await api(path, payload);
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
  const date = new Date(Number(seconds) * 1000);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
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
    case "overview":
      renderWhenChanged("overview", {
        nodes: appState.nodes,
        subscriptions: appState.subscriptions,
        bridges: appState.bridges,
        tests: appState.tests,
        systemLogs: appState.systemLogs,
        logCenter: appState.logCenter,
        cores: appState.cores,
        sync: appState.sync,
      }, renderOverview, force);
      break;
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
    case "pool":
      renderWhenChanged("pool", { pool: appState.pool, gateway: appState.smartGateway, docker: dockerRuntime() }, () => {
        renderPool();
      }, force);
      break;
    case "browsers":
      renderWhenChanged("browsers", { instances: appState.browser_instances, nodes: appState.nodes, filter: browserPageFilter, selected: [...appState.selectedBrowserIds], sync: appState.sync?.enabled === true }, renderBrowserPage, force);
      break;
    case "cores":
      renderKernelPage();
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

function applyStatusSnapshot(status) {
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
  appState.fingerprint_kernels = Array.isArray(status.fingerprint_kernels) ? status.fingerprint_kernels : [];
  appState.fingerprint_default_kernel = status.fingerprint_default_kernel || "";
  appState.browser_extensions = Array.isArray(status.browser_extensions) ? status.browser_extensions : [];
  appState.fingerprint_check_sites = status.fingerprint_check_sites || {};
  appState.active_browsers = status.active_browsers || [];
  appState.browser_instances = status.browser_instances || [];
  const subscriptionUrls = new Map((status.subscriptions || []).map((item) => [item.id, item.url]));
  for (const item of appState.subscriptions) {
    if (subscriptionUrls.get(item.id) !== item.url) revealedSecrets.delete(`subscription:${item.id}`);
  }
  appState.subscriptions = status.subscriptions || [];
  appState.nodes = status.nodes || [];
  appState.regions = Array.isArray(status.regions) ? status.regions : appState.regions;
  appState.bridges = status.bridges || [];
  appState.tests = status.tests || {};
  appState.speedTest = status.speed_test || { settings: null, tasks: [] };
  appState.subscriptionSettings = status.subscription_settings || { allow_non_public_urls: false };
  appState.subscriptionRefresh = status.subscription_refresh || { tasks: [] };
  appState.smartGateway = status.smart_gateway || null;
  appState.logCenter = status.logs || appState.logCenter;
  if (appState.activeView !== "logs" || !appState.requestLogPage.loaded) appState.requestLogs = status.request_logs || [];
  if (appState.activeView !== "system" || !appState.systemLogPage.loaded) appState.systemLogs = status.system_logs || [];
  if (appState.pool?.base_url !== status.pool?.base_url) {
    for (const key of revealedSecrets) if (key.startsWith("pool:")) revealedSecrets.delete(key);
  }
  appState.pool = status.pool || null;
  if (status.network && typeof status.network === "object") {
    appState.network = status.network;
    appState.networkStatus = "ready";
  } else appState.networkStatus = "error";
  appState.sync = status.sync || appState.sync || null;
  appState.healthSettings = status.health_settings || appState.healthSettings || { auto_switch: false };
  if ("sync_openssl" in status) appState.syncOpenssl = Boolean(status.sync_openssl);
  render();
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
      applyStatusSnapshot(status);
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
      logs: Number(appState.logCenter?.system_error_total ?? appState.systemLogs.filter((entry) => /error|critical|错误|致命/i.test(String(entry.level || entry.severity || ""))).length),
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
  refreshBrowserConfigTools();
  const footerStatus = $("#footerStatus");
  if (footerStatus) footerStatus.textContent = `${appState.cores["sing-box"] || appState.cores.xray ? "核心正常" : "核心待安装"} · 局域网${appState.network?.allow_lan ? "已开启" : "关闭"} · ${appState.sync?.last_push_at ? `同步 ${formatRelativeTime(appState.sync.last_push_at)}` : "同步未运行"}`;
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
  i18n.apply(document);
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
  const subscriptionIds = new Set(appState.subscriptions.map((item) => item.id));
  // 已删除的订阅不能因菜单或规则编辑仍打开而被 keyed diff 延后移除。
  for (const card of list.querySelectorAll("[data-subscription-id]")) {
    if (subscriptionIds.has(card.dataset.subscriptionId)) continue;
    if (card.contains(document.activeElement)) {
      $('.view[data-view="subscriptions"] [data-action="open-subscription-drawer"]')?.focus();
    }
    card.remove();
  }
  const count = $("#subsCount");
  if (count) count.textContent = `${appState.subscriptions.length} 个订阅`;
  const allowNonPublic = $("#allowNonPublicSubscriptions");
  if (allowNonPublic) {
    allowNonPublic.checked = Boolean(appState.subscriptionSettings?.allow_non_public_urls);
  }
  renderSubscriptionRefreshTask();
  const securityBadge = $("#nonPublicSubscriptionBadge");
  if (securityBadge) securityBadge.hidden = !appState.subscriptionSettings?.allow_non_public_urls;
  if (!appState.subscriptions.length) {
    uiCore.patchKeyedHtml(list, '<div class="subscription-empty" data-ui-key="subscriptions:empty">暂无订阅，粘贴订阅 URL 或原始链接后即可导入。</div>', { keyAttribute: "data-ui-key" });
    return;
  }
  uiCore.patchKeyedHtml(list, appState.subscriptions
    .map(
      (item) => `
        <article class="subscription-item" data-ui-key="subscription:${escapeHtml(item.id)}" data-subscription-id="${escapeHtml(item.id)}">
          <div class="subscription-head">
            <span class="sub-avatar">${escapeHtml(initials(item.name))}</span>
            <div class="subscription-copy">
              <div class="subscription-title-row">
                <strong class="subscription-name">${escapeHtml(item.name)}</strong>
                <span class="sub-status ${item.last_error ? "is-error" : ""}">${item.last_error ? "需检查" : "正常"}</span>
              </div>
              <div class="subscription-meta">${item.node_count || 0} 个节点 · 更新于 ${escapeHtml(formatRelativeTime(item.last_update))}</div>
              ${item.url ? `<div class="subscription-sensitive-url"><code class="subscription-url" title="${revealedSecrets.has(`subscription:${item.id}`) ? "完整地址已显示" : "地址已脱敏"}">${escapeHtml(revealedSecrets.has(`subscription:${item.id}`) ? item.url : uiCore.maskSensitiveUrl(item.url))}</code><button class="icon-button" type="button" data-action="reveal-subscription-url" data-id="${escapeHtml(item.id)}" aria-label="显示或隐藏订阅 URL">◎</button></div>` : '<div class="subscription-url">手动导入</div>'}
              ${renderSubscriptionUsage(item)}
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
            <details class="row-more"><summary class="icon-button" aria-label="更多订阅操作">⋯</summary><div><button type="button" data-action="copy-subscription-url" data-id="${escapeHtml(item.id)}">复制 URL</button><button type="button" data-action="delete-sub" data-id="${escapeHtml(item.id)}">删除订阅</button></div></details>
          </div>
          <details class="subscription-rules"><summary>节点过滤规则${item.filter_include || item.filter_exclude ? " · 已启用" : ""}</summary><div class="form-grid"><label><span>包含关键词（每行一个）</span><textarea data-sub-filter="include" data-id="${escapeHtml(item.id)}" rows="2">${escapeHtml(item.filter_include || "")}</textarea></label><label><span>排除关键词（每行一个）</span><textarea data-sub-filter="exclude" data-id="${escapeHtml(item.id)}" rows="2">${escapeHtml(item.filter_exclude || "")}</textarea></label><label class="sync-checkbox sync-span-2"><input type="checkbox" data-sub-filter="regex" data-id="${escapeHtml(item.id)}" ${item.filter_regex ? "checked" : ""}><span>将规则作为正则表达式处理</span></label></div><button class="toolbar-button" type="button" data-action="save-subscription-filters" data-id="${escapeHtml(item.id)}">保存过滤规则</button></details>
          ${renderSubscriptionHistory(item)}
        </article>
      `
    )
    .join(""), { keyAttribute: "data-ui-key" });
}

function renderSubscriptionUsage(item) {
  const usage = item.usage;
  if (!usage) return "";
  const hasUsedBytes = usage.upload != null && usage.download != null;
  const hasTotal = usage.total != null;
  const used = hasUsedBytes ? Number(usage.upload) + Number(usage.download) : null;
  const total = hasTotal ? Math.max(0, Number(usage.total)) : null;
  const percent = total > 0 && used != null ? Math.max(0, Math.min(100, used / total * 100)) : null;
  const days = usage.expire != null && Number(usage.expire) > 0 ? Math.ceil((Number(usage.expire) - Date.now() / 1000) / 86400) : null;
  if (used == null && total == null && days == null) return "";
  const usedLabel = used == null ? "已用量未知" : `${escapeHtml(formatBytes(used))} 已用`;
  const totalLabel = total == null ? "总量未知" : escapeHtml(formatBytes(total));
  const usageMarkup = used != null || total != null
    ? `<div><span>流量</span><strong>${usedLabel} / ${totalLabel}</strong>${percent != null ? `<span>${percent.toFixed(0)}%</span>` : ""}</div>${percent != null ? `<div class="usage-track"><span style="width:${percent}%"></span></div>` : ""}`
    : `<div><span>流量</span><strong>流量信息不完整</strong></div>`;
  const expiryMarkup = days != null ? `<small class="${days <= 7 ? "text-warning" : ""}">${days < 0 ? "已到期" : `剩余 ${days} 天`}</small>` : "";
  return `<div class="subscription-usage">${usageMarkup}${expiryMarkup}</div>`;
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

function updateOptionsMarkup(select, signature, markup, validValues, desiredValue) {
  if (!select) return "all";
  const current = select.value;
  if (select.dataset.optionsSignature !== signature) {
    if (document.activeElement === select) {
      if (select.dataset.refreshOptionsOnBlur !== "true") {
        select.dataset.refreshOptionsOnBlur = "true";
        select.addEventListener("blur", () => requestAnimationFrame(() => {
          delete select.dataset.refreshOptionsOnBlur;
          if (appState.activeView === "nodes") {
            renderFilters();
            renderNodes();
          } else if (appState.activeView === "browsers") renderBrowserPage();
          else if (appState.activeView === "logs" || appState.activeView === "system") renderActiveView({ force: true });
        }), { once: true });
      }
      return validValues.includes(current) ? current : "all";
    }
    select.innerHTML = markup;
    select.dataset.optionsSignature = signature;
  }
  if (document.activeElement !== select) select.value = validValues.includes(desiredValue) ? desiredValue : "all";
  return validValues.includes(select.value) ? select.value : "all";
}

function setSelectOptions(select, values, current, allLabel) {
  const normalized = ["all", ...values.filter(Boolean).sort((a, b) => a.localeCompare(b, "zh-CN"))];
  const signature = JSON.stringify(normalized);
  const markup = normalized
    .map((value) => `<option value="${escapeHtml(value)}">${value === "all" ? allLabel : escapeHtml(value)}</option>`)
    .join("");
  return updateOptionsMarkup(select, signature, markup, normalized, current);
}

function renderFilters() {
  const protocols = [...new Set(appState.nodes.map((node) => node.protocol))];
  const regions = [...new Set(appState.nodes.map((node) => node.region))];
  appState.filters.protocol = setSelectOptions($("#protocolFilter"), protocols, appState.filters.protocol, "全部协议");
  appState.filters.region = setSelectOptions($("#regionFilter"), regions, appState.filters.region, "全部地区");
  const sortSelect = $("#nodeSort");
  if (sortSelect && document.activeElement !== sortSelect) sortSelect.value = appState.filters.sort || "name";

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
    const allowed = new Set(["all", "__other__", ...(appState.subscriptions || []).map((s) => s.id)]);
    if (!allowed.has(appState.filters.subscription)) {
      appState.filters.subscription = "all";
    }
    const signature = JSON.stringify((appState.subscriptions || []).map((sub) => [sub.id, sub.name, sub.node_count, sub.node_ids?.length]));
    appState.filters.subscription = updateOptionsMarkup(
      subSelect,
      signature,
      options.join(""),
      [...allowed],
      appState.filters.subscription,
    );
  }

  const search = $("#searchInput");
  if (document.activeElement !== search) search.value = appState.filters.search;
}

function bridgeForNode(nodeId) {
  return appState.bridges.find((bridge) => bridge.node_id === nodeId && ["running", "starting"].includes(bridge.status));
}

function mostRecentBridgeForNode(nodeId) {
  let latest = null;
  let latestAt = -Infinity;
  for (const bridge of appState.bridges || []) {
    if (bridge.node_id !== nodeId) continue;
    const timestamp = Number(bridge.started_at || bridge.created_at || 0);
    if (timestamp >= latestAt) {
      latest = bridge;
      latestAt = timestamp;
    }
  }
  return latest;
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
  const logFilterOptions = [
    '<option value="all">全部节点</option>',
    ...options.map(([id, name]) => `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`),
  ].join("");
  const logFilterValues = ["all", ...nodeOptions.keys()];
  if (!logFilterValues.includes(appState.logFilters.node)) appState.logFilters.node = "all";
  appState.logFilters.node = updateOptionsMarkup(select, JSON.stringify([...nodeOptions]), logFilterOptions, logFilterValues, appState.logFilters.node);
  const statusFilter = $("#logStatusFilter");
  if (document.activeElement !== statusFilter) statusFilter.value = ["all", "connecting", "connected", "closed", "failed"].includes(appState.logFilters.status) ? appState.logFilters.status : "all";
  const search = $("#logSearchInput");
  if (document.activeElement !== search) search.value = appState.logFilters.search;
}

function filteredRequestLogs() {
  return appState.requestLogs || [];
}

function latencyCell(nodeId) {
  const result = appState.tests[nodeId];
  if (!result) return `<button class="latency-badge is-idle" data-action="test" data-node="${escapeHtml(nodeId)}" title="点击测速">未测试</button>`;
  if (!result.ok) return `<button class="latency-badge is-error" data-action="test" data-node="${escapeHtml(nodeId)}" title="${escapeHtml(result.message || "测速失败")}">失败 · 重测</button>`;
  if (!Number.isFinite(result.latency_ms) || result.latency_ms < 0) return `<button class="latency-badge is-idle" data-action="test" data-node="${escapeHtml(nodeId)}" title="点击重测" data-i18n>无延迟数据</button>`;
  const ms = Number(result.latency_ms);
  const speed = ms < LATENCY_WARNING_MS ? "is-ok" : ms < LATENCY_SLOW_MS ? "is-warn" : "is-slow";
  const rate = Math.round(Number(result.success_rate || 0) * 100);
  const age = result.age_seconds == null ? "未知" : `${formatDuration(result.age_seconds)}前`;
  return `<button class="latency-badge ${speed} ${result.stale ? "is-stale" : ""}" data-action="test" data-node="${escapeHtml(nodeId)}" title="成功率 ${rate}% · ${escapeHtml(age)}${result.message ? ` · ${escapeHtml(result.message)}` : ""}">${ms} ms${result.stale ? " · 过期" : ""}</button>`;
}

function filteredNodes() {
  const search = appState.filters.search.trim().toLowerCase();
  const subFilter = appState.filters.subscription || "all";
  const byNode = subscriptionIndexByNodeId();
  const latestBridges = new Map();
  for (const bridge of appState.bridges || []) {
    const previous = latestBridges.get(bridge.node_id);
    const timestamp = Number(bridge.started_at || bridge.created_at || 0);
    if (!previous || timestamp >= Number(previous.started_at || previous.created_at || 0)) latestBridges.set(bridge.node_id, bridge);
  }
  const nodes = appState.nodes.filter((node) => {
    const bridge = bridgeForNode(node.id);
    const result = appState.tests[node.id];
    const latestBridge = latestBridges.get(node.id);
    const healthError = bridge?.health?.status === "error" || (!bridge && latestBridge?.health?.status === "error");
    if (appState.filters.status === "running" && !bridge) return false;
    if (appState.filters.status === "available" && (!result || !result.ok || result.stale || healthError)) return false;
    if (appState.filters.status === "failed" && (!healthError && (!result || result.ok))) return false;
    if (appState.filters.status === "untested" && result) return false;
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
    if (Boolean(left.favorite) !== Boolean(right.favorite)) return left.favorite ? -1 : 1;
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


// 绑定节点行的复选框事件
function bindNodeCheckboxes() {
  // 处理单个节点复选框
  document.querySelectorAll(".node-checkbox").forEach((checkbox) => {
    if (checkbox.dataset.changeBound) return;
    checkbox.dataset.changeBound = "true";
    checkbox.addEventListener("change", (e) => {
      const nodeId = e.target.dataset.nodeId;
      if (e.target.checked) {
        appState.selectedNodeIds.add(nodeId);
      } else {
        appState.selectedNodeIds.delete(nodeId);
      }
      updateBatchActionsBar();
      // 更新行样式
      const row = e.target.closest("tr.node-row");
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
        const row = checkbox.closest("tr.node-row");
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
  let successCount = 0;
  const failures = [];

  // 依次启动选中节点，保留各节点的失败原因供用户处理。
  for (const nodeId of selectedNodeIds) {
    try {
      await api("/api/bridges/start", { node_id: nodeId, core: "auto" });
      successCount++;
    } catch (error) {
      console.error(`启动节点 ${nodeId} 失败:`, error);
      failures.push({ nodeId, name: nodeById(nodeId)?.name || nodeId, error: error.message || String(error) });
    }
    // 添加小延迟避免过快请求
    await new Promise(resolve => setTimeout(resolve, 100));
  }

  // 刷新状态
  await loadStatus({ forceFresh: true }).catch((error) => {
    console.error("刷新状态失败:", error);
  });

  // 显示结果提示
  if (failures.length) {
    showToast(`批量启动完成：成功 ${successCount} 个，失败 ${failures.length} 个`, "error");
    showBatchStartResults(successCount, failures);
  } else showToast(`成功启动 ${successCount} 个节点`);

  // 清除选择
  clearNodeSelections();
}

function showBatchStartResults(successCount, failures) {
  const dialog = $("#batchStartResultDialog");
  if (!dialog) return;
  $("#batchStartResultSummary").textContent = `成功启动 ${successCount} 个节点，${failures.length} 个失败。`;
  $("#batchStartFailureList").innerHTML = failures.map((item) => `<li><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.error)}</span></li>`).join("");
  dialog.hidden = false;
  dialog.querySelector('[role="dialog"]')?.focus();
}

function closeBatchStartResults() {
  $("#batchStartResultDialog").hidden = true;
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
  const members = Array.isArray(pool.members) ? pool.members : [];
  const memberCount = Number(pool.member_count ?? members.length ?? 0);
  const countEl = $("#poolMemberCount");
  if (countEl) countEl.textContent = `${memberCount} 个成员`;
  if (poolLinksExpanded == null) poolLinksExpanded = memberCount > 0;
  const poolShowLinks = $("#poolShowLinks");
  const poolAccessLinks = $("#poolAccessLinks");
  const poolEmptyGuide = $("#poolEmptyGuide");
  const poolAccessUrl = $("#poolAccessUrl");
  const linkByFormat = {
    list_json: links.list_json || "",
    proxies_txt: links.proxies_txt || "",
    get_health: links.get_health || links.get_random || "",
  };
  const visibleUrl = revealedSecrets.has(`pool:${currentPoolLinkFormat}`);
  const selectedUrl = linkByFormat[currentPoolLinkFormat] || "";
  if (poolShowLinks) {
    poolShowLinks.hidden = !Object.values(linkByFormat).some(Boolean);
    poolShowLinks.textContent = poolLinksExpanded ? "隐藏访问链接" : "显示访问链接";
    poolShowLinks.setAttribute("aria-expanded", poolLinksExpanded ? "true" : "false");
    poolShowLinks.title = memberCount ? "显示或隐藏访问链接" : "启动节点后成员会自动加入，也可查看空池链接";
  }
  if (poolAccessLinks) poolAccessLinks.hidden = !poolLinksExpanded;
  if (poolEmptyGuide) poolEmptyGuide.textContent = memberCount
    ? "运行中的代理会自动加入，可按需临时排除成员。"
    : "启动节点后会自动加入代理池。";
  if (poolAccessUrl && document.activeElement !== poolAccessUrl) {
    poolAccessUrl.value = selectedUrl ? (visibleUrl ? selectedUrl : uiCore.maskSensitiveUrl(selectedUrl)) : "";
    poolAccessUrl.title = visibleUrl ? "敏感访问链接已显示" : "访问链接中的密钥已隐藏";
  }
  document.querySelectorAll("[data-pool-format]").forEach((button) => {
    const active = button.dataset.poolFormat === currentPoolLinkFormat;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  const revealButton = $("#poolRevealLink");
  if (revealButton) {
    revealButton.textContent = visibleUrl ? "◉" : "◎";
    revealButton.setAttribute("aria-label", visibleUrl ? "隐藏访问链接" : "显示访问链接");
    revealButton.title = visibleUrl ? "隐藏敏感链接" : "显示完整链接";
  }

  const memberList = $("#poolMemberList");
  if (!memberList) return;
  if (!members.length) {
    memberList.innerHTML = "";
  } else {
    memberList.innerHTML = members.map((member) => {
    const state = member.circuit_state || "closed";
      const excludedRemaining = Math.max(0, Number(member.excluded_until || 0) - Date.now() / 1000);
      const label = state === "excluded" ? `已排除 · ${Math.ceil(excludedRemaining / 60)} 分钟` : state === "open" ? `熔断 · ${Math.ceil(excludedRemaining)} 秒` : state === "half_open" ? "恢复探测" : "健康";
      const latency = member.latency_ms == null ? "未测速" : `${Number(member.latency_ms)} ms`;
      const successRate = member.success_rate == null ? "成功率未知" : `成功率 ${Math.round(Number(member.success_rate) * 100)}%`;
      const action = state === "excluded"
        ? `<button class="row-button" type="button" data-action="exclude-pool-member" data-id="${escapeHtml(member.id || "")}" data-duration="0">恢复</button>`
        : `<button class="row-button" type="button" data-action="exclude-pool-member" data-id="${escapeHtml(member.id || "")}" data-duration="1800">排除 30 分钟</button>`;
      return `
        <div class="pool-member-row is-${escapeHtml(state)}">
          <div>
            <strong>${escapeHtml(member.node_name || "未命名节点")}</strong>
            <span>${escapeHtml(label)} · 健康 ${Number(member.health_score || 0)} · ${escapeHtml(latency)} · ${escapeHtml(successRate)}</span>
            ${member.last_error ? `<small class="text-error">${escapeHtml(member.last_error)}</small>` : ""}
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
  if (document.activeElement !== strategy) strategy.value = gateway.strategy || "health";
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
  status.textContent = gateway.running ? `运行中 · ${Number(gateway.available_members || 0)} 个节点 · ${Number(gateway.client_count || 0)} 个连接` : (gateway.last_error || "未运行");
  save.textContent = gateway.running ? "保存设置" : "保存并启动";
  save.disabled = false;
  stop.disabled = !gateway.running;
  stop.hidden = !gateway.running;
  if (links) {
    const allocations = Array.isArray(gateway.allocations) ? gateway.allocations : [];
    const switches = Array.isArray(gateway.recent_switches) ? gateway.recent_switches.slice(0, 5) : [];
    links.innerHTML = gateway.running
      ? `<div class="pool-link-row"><span class="pool-link-label">SOCKS5</span><code class="pool-link-url">${escapeHtml(gateway.socks_url || "")}</code></div><div class="pool-link-row"><span class="pool-link-label">HTTP</span><code class="pool-link-url">${escapeHtml(gateway.http_url || "")}</code></div><div class="gateway-observability"><strong>节点分配</strong>${allocations.length ? allocations.map((item) => `<span>${escapeHtml(item.node_name || item.name || "节点")} · ${Number(item.connections || item.count || 0)} 次 · ${Math.round(Number(item.share || item.ratio || 0) * 100)}%</span>`).join("") : "<span>暂无连接分配记录</span>"}<strong>最近切换</strong>${switches.length ? switches.map((item) => `<span>${escapeHtml(item.from_node || appState.bridges.find((bridge) => bridge.id === item.from)?.node_name || "起始")} → ${escapeHtml(item.to_node || item.node_name || appState.bridges.find((bridge) => bridge.id === item.to)?.node_name || "节点")} · ${escapeHtml(formatLogTime(item.at || item.timestamp))}</span>`).join("") : "<span>暂无切换事件</span>"}</div>`
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
    uiCore.patchKeyedHtml(list, `
      <div class="bridge-empty" data-ui-key="bridge:empty">
        <div class="status-orb idle">${icon("minus")}</div>
        <strong>暂无运行中的本地代理</strong>
        <span>在「节点」里启动一个节点后，它会在这里显示为一张独立的端口卡片。同一端口同时支持 SOCKS5、HTTP 代理和 HTTPS CONNECT。</span>
      </div>
    `, { keyAttribute: "data-ui-key" });
    return;
  }

  uiCore.patchKeyedHtml(list, bridges.map((bridge) => {
    const endpoint = endpointParts(bridge.socks_url, bridge.port);
    const listenAddress = bridge.listen_host || endpoint.address;
    const healthError = bridge.health?.status === "error";
    const httpUrl = bridge.http_url || `http://127.0.0.1:${bridge.port}`;
    const uploadBytes = Number(bridge.upload_bytes || 0);
    const downloadBytes = Number(bridge.download_bytes || 0);
    const totalBytes = Number(bridge.total_bytes || uploadBytes + downloadBytes);
    return `
      <article class="bridge-card ${healthError ? "is-unhealthy" : ""}" data-ui-key="bridge:${escapeHtml(bridge.id)}" data-bridge-id="${escapeHtml(bridge.id)}">
        <header class="bridge-card-head">
          <div class="status-orb ${bridge.status === "running" ? "" : "idle"}">${icon(bridge.status === "running" ? "check" : "minus")}</div>
          <div class="bridge-card-title">
            <div class="bridge-card-name">
              <strong>${escapeHtml(bridge.node_name)}</strong>
              <span class="status-pill ${healthError ? "is-error" : "is-ok"}">${healthError ? "探测异常" : "运行中"}</span>
              <span class="bridge-runtime">${escapeHtml(formatDuration(bridge.runtime_seconds || Math.floor(Date.now() / 1000 - Number(bridge.started_at || Date.now() / 1000))))}</span>
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
          ${appState.network?.allow_lan ? `<div class="endpoint-row">
            <div>
              <span class="endpoint-label">监听地址</span>
              <span class="endpoint-value">${escapeHtml(listenAddress)}</span>
            </div>
            <button class="row-button copy-icon" data-action="copy" data-text="${escapeHtml(listenAddress)}" aria-label="复制监听地址" title="复制监听地址">${icon("copy")}</button>
          </div>` : ""}
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
          <details class="row-more"><summary class="icon-button" aria-label="更多复制格式">⋯</summary><div><button type="button" data-action="copy-bridge-format" data-node="${escapeHtml(bridge.node_id)}" data-format="env">复制环境变量</button><button type="button" data-action="copy-bridge-format" data-node="${escapeHtml(bridge.node_id)}" data-format="curl">复制 curl 示例</button><button type="button" data-action="copy-bridge-format" data-node="${escapeHtml(bridge.node_id)}" data-format="clash">复制 Clash 片段</button></div></details>
        </div>
      </article>
    `;
  }).join(""), { keyAttribute: "data-ui-key" });
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
  const systemSearch = $("#systemSearchInput");
  if (document.activeElement !== systemSearch) systemSearch.value = appState.systemLogFilters.search;
  const systemLevel = $("#systemLevelFilter");
  if (document.activeElement !== systemLevel) systemLevel.value = ["all", "info", "warn", "error"].includes(appState.systemLogFilters.level)
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
    && $("#browserConfigModal").hidden
    && $("#kernelReleaseModal").hidden
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
  $("#remoteBrowserLaunchTitle").textContent = result.batch_count > 1
    ? `已启动 ${result.batch_count} 个远程浏览器`
    : `${result.browser_name || "远程浏览器"} 已启动`;
  $("#remoteBrowserLaunchMeta").textContent = result.batch_count > 1
    ? `${credentials.username} · 本批次使用同一访问凭据 · 可在指纹浏览器页查看所有 Worker`
    : `${credentials.username} · ${remoteUrl}`;
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
  const batchCount = pendingRemoteBrowserBatchInstances.length || pendingRemoteBrowserBatchNodeIds.length;
  $("#remoteBrowserLaunchTitle").textContent = batchCount > 1
    ? `启动 ${batchCount} 个远程浏览器`
    : instance
    ? `启动 ${instance.name || "远程浏览器"}`
    : "启动远程浏览器";
  $("#remoteBrowserLaunchMeta").textContent = pendingRemoteBrowserBatchInstances.length > 1
    ? `为 ${batchCount} 条所选记录各建独立 Worker，复用各自 Profile；同一组访问凭据会用于本批次。`
    : batchCount > 1
    ? `为 ${batchCount} 个所选节点各建独立 Worker 与 Profile；同一组访问凭据会用于本批次。`
    : `${node.name || nodeId} · ${instance ? "复用独立 Profile" : "新建独立 Worker 与 Profile"}`;
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
  pendingRemoteBrowserBatchNodeIds = [];
  pendingRemoteBrowserBatchInstances = [];
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
            <span>指纹</span><code>${escapeHtml(instance.fingerprint_seed || "-")} · ${escapeHtml(browserConfig.fingerprintSummary(instance))}</code>
            ${instance.group ? `<span>分组</span><code>${escapeHtml(instance.group)}</code>` : ""}
            ${running && instance.runtime_locale?.timezone ? `<span>时区 / 语言</span><code>${escapeHtml(instance.runtime_locale.timezone)} · ${escapeHtml(instance.runtime_locale.language || "")}（${escapeHtml(browserConfig.localeSourceLabel(instance.runtime_locale.timezone_source))}）</code>` : ""}
            ${running && instance.runtime_kernel ? `<span>运行内核</span><code>${escapeHtml(instance.runtime_kernel)}</code>` : instance.kernel_version ? `<span>固定内核</span><code>${escapeHtml(instance.kernel_version)}</code>` : ""}
            ${browserConfig.cdpEndpoint(instance) ? `<span>CDP</span><code title="${escapeHtml(browserConfig.cdpEndpoint(instance))}">${escapeHtml(browserConfig.cdpEndpoint(instance))}</code>` : ""}
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
            <button class="row-button" type="button" data-action="open-browser-config" data-instance="${escapeHtml(instance.id)}" aria-label="配置 ${escapeHtml(instance.name || "浏览器")}">
              <span>配置</span>
            </button>
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
  if (!await confirmAction(
    `删除「${instance.name || "浏览器"}」会先停止对应浏览器/Worker，并永久删除该记录的 Profile、Cookie 和本地存储；此操作无法恢复。${syncNotice}`,
    { title: "永久删除浏览器 Profile", confirmLabel: "永久删除", details: "该 Profile 中保存的 Cookie、登录态和本地数据无法恢复。" }
  )) return null;
  const result = await api("/api/browser/delete", { instance_id: instanceId });
  await loadStatus({ forceFresh: true });
  if (appState.browserRecordNodeId) renderBrowserRecords(appState.browserRecordNodeId);
  showToast(syncEnabled ? "浏览器记录与本地数据已删除，正在同步清理 WebDAV" : "浏览器记录与本地数据已永久删除");
  return result;
}

// ============ 浏览器配置：新建 / 编辑 / 批量配置 / 换绑 ============
const ACTIVE_BROWSER_STATUS = ["running", "starting", "stopping"];

function selectOptionsMarkup(options, selected, { keep = false } = {}) {
  const keepOption = keep ? `<option value="${browserConfig.KEEP}" selected>保持不变</option>` : "";
  return keepOption + options.map(([value, label]) => `<option value="${escapeHtml(value)}" ${!keep && String(value) === String(selected ?? "") ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function browserNodeOptionsMarkup(selectedId = "") {
  const nodes = appState.nodes.filter((node) => !node.browser_preserved || node.id === selectedId);
  return nodes.map((node) => `<option value="${escapeHtml(node.id)}" ${node.id === selectedId ? "selected" : ""}>${escapeHtml(node.name || node.id)}${node.region_code && node.region_code !== "ZZ" ? ` · ${escapeHtml(node.region_code)}` : ""}${node.browser_preserved ? "（已从订阅移除）" : ""}</option>`).join("");
}

function kernelOptionsMarkup(selected = "", { keep = false } = {}) {
  const kernels = appState.fingerprint_kernels || [];
  const defaultKernel = kernels.find((kernel) => kernel.default);
  const label = (kernel) => `${kernel.version || kernel.label || kernel.id}${kernel.id === "legacy" ? " · 早期安装" : kernel.id === "env" ? " · 环境变量" : ""}`;
  const missing = selected && !kernels.some((kernel) => kernel.id === selected)
    ? `<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)}（本机未安装）</option>`
    : "";
  return `${keep ? `<option value="${browserConfig.KEEP}" selected>保持不变</option>` : ""}<option value="" ${!keep && !selected ? "selected" : ""}>跟随默认版本${defaultKernel ? `（${escapeHtml(label(defaultKernel))}）` : "（未安装）"}</option>${kernels.map((kernel) => `<option value="${escapeHtml(kernel.id)}" ${!keep && kernel.id === selected ? "selected" : ""}>${escapeHtml(label(kernel))}</option>`).join("")}${keep ? "" : missing}`;
}

function browserConfigField(label, control, hint = "") {
  return `<label class="core-upload-field"><span>${escapeHtml(label)}</span>${control}${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</label>`;
}

function renderBrowserConfigBasic(mode, instance) {
  if (mode === "rebind") {
    const ids = browserConfigState.instanceIds;
    return `<section data-config-panel="basic"><p class="core-upload-description">将 ${ids.length} 条已停止的记录移到目标节点，Profile 目录随之移动，指纹种子与 Cookie 不变。运行中的记录需先停止。</p>${browserConfigField("目标节点", `<select name="node_id" required>${browserNodeOptionsMarkup("")}</select>`)}</section>`;
  }
  if (mode === "batch") {
    return `<section data-config-panel="basic"><p class="core-upload-description">将修改 ${browserConfigState.instanceIds.length} 条记录。选择「保持不变」的项不会提交；运行中的记录在下次启动时生效。</p><label class="sync-checkbox"><input type="checkbox" name="groupEnabled"><span>修改分组</span></label>${browserConfigField("分组", `<input name="group" maxlength="40" placeholder="留空表示移出分组">`)}<label class="sync-checkbox"><input type="checkbox" name="labelEnabled"><span>修改标签</span></label>${browserConfigField("标签", `<input name="label" maxlength="120" placeholder="留空表示清除标签">`)}</section>`;
  }
  const running = instance && ACTIVE_BROWSER_STATUS.includes(instance.status);
  const nodeSelect = mode === "create"
    ? browserConfigField("绑定节点", `<select name="node_id" required>${browserNodeOptionsMarkup(browserConfigState.nodeId || "")}</select>`, "浏览器通过该节点的本地代理上网；未启动的节点会在打开浏览器时自动启动。")
    : browserConfigField("绑定节点", `<select name="node_id" ${running || instance?.legacy ? "disabled" : ""}>${browserNodeOptionsMarkup(instance?.node_id || "")}</select>`, instance?.legacy ? "早期默认浏览器记录不支持换绑。" : running ? "停止浏览器后才能换绑节点。" : "换绑后 Profile 随记录移动，指纹种子不变。");
  return `<section data-config-panel="basic">${nodeSelect}${mode === "create"
    ? browserConfigField("新建数量", `<input name="count" type="number" min="1" max="50" value="1" inputmode="numeric">`, "每条记录拥有独立的指纹种子与 Profile。")
    : browserConfigField("名称", `<input name="name" maxlength="40" value="${escapeHtml(instance?.name || "")}" required>`)}${browserConfigField("标签", `<input name="label" maxlength="120" value="${escapeHtml(instance?.label || "")}" placeholder="例如：访问 xx 的浏览器">`)}${browserConfigField("分组", `<input name="group" maxlength="40" list="browserGroupSuggestions" value="${escapeHtml(instance?.group || "")}" placeholder="例如：电商店铺"><datalist id="browserGroupSuggestions">${browserConfig.browserGroups(appState.browser_instances).map((group) => `<option value="${escapeHtml(group)}"></option>`).join("")}</datalist>`)}${browserConfigField("启动网址", `<textarea name="start_urls" rows="3" placeholder="每行一个 http(s) 地址；留空则首次打开出口检测页">${escapeHtml((instance?.start_urls || []).join("\n"))}</textarea>`, "最多 10 个，每次启动时打开。")}</section>`;
}

function renderBrowserConfigFingerprint(mode, instance) {
  const fp = mode === "create" ? {} : browserConfig.fingerprintOf(instance);
  const keep = mode === "batch";
  const value = (key, fallback = "") => escapeHtml(fp[key] ?? fallback);
  const createDefaults = { platform: "auto", brand: "Chrome", hardware_concurrency: 0 };
  const current = (key) => mode === "create" ? createDefaults[key] : fp[key];
  const running = instance && ACTIVE_BROWSER_STATUS.includes(instance.status);
  const seed = mode === "edit"
    ? `<div class="browser-config-seed"><span>指纹种子</span><code>${escapeHtml(instance?.fingerprint_seed || "-")}</code><button class="row-button" type="button" data-action="regenerate-browser-fingerprint" data-instance="${escapeHtml(instance?.id || "")}" ${running ? "disabled title=\"停止后才能重新生成\"" : ""}>重新生成指纹</button></div><small class="browser-config-note">Canvas、WebGL、Audio、字体等特征由种子派生。重新生成后，已登录网站会把它视为一台新设备。</small>`
    : "";
  const textFields = keep ? "" : `${browserConfigField("平台版本", `<input name="platform_version" value="${value("platform_version")}" placeholder="留空使用内核默认值，例如 10.0.0">`)}${browserConfigField("品牌版本", `<input name="brand_version" value="${value("brand_version")}" placeholder="留空跟随内核版本">`)}${browserConfigField("窗口尺寸", `<input name="window_size" value="${value("window_size")}" placeholder="留空自动，例如 1920x1080">`)}`;
  const disabled = new Set(fp.disable_spoofing || []);
  const spoofing = keep ? "" : `<fieldset class="browser-config-checks"><legend>关闭伪装项</legend>${browserConfig.SPOOFING_OPTIONS.map(([key, label]) => `<label class="sync-checkbox"><input type="checkbox" name="disable_spoofing" value="${escapeHtml(key)}" ${disabled.has(key) ? "checked" : ""}><span>${escapeHtml(label)}</span></label>`).join("")}<small>仅在网站兼容异常时关闭；需要内核 144 及以上版本。</small></fieldset>`;
  return `<section data-config-panel="fingerprint" hidden>${seed}${browserConfigField("操作系统", `<select name="platform">${selectOptionsMarkup(browserConfig.PLATFORM_OPTIONS, current("platform"), { keep })}</select>`, "选择与真实系统不同的平台时，字体等特征仍来自真实系统，可能被识别为不一致。")}${browserConfigField("浏览器品牌", `<select name="brand">${selectOptionsMarkup(browserConfig.BRAND_OPTIONS, current("brand") ?? "", { keep })}</select>`, "Chrome 是真实用户最常见的品牌；Chromium 品牌较少见。")}${browserConfigField("CPU 核心数", `<select name="hardware_concurrency">${selectOptionsMarkup(browserConfig.HARDWARE_OPTIONS, String(current("hardware_concurrency") || 0), { keep })}</select>`)}${textFields}${spoofing}</section>`;
}

function renderBrowserConfigLocale(mode, instance) {
  const fp = mode === "create" ? {} : browserConfig.fingerprintOf(instance);
  const keep = mode === "batch";
  const modes = keep ? browserConfig.LOCALE_MODE_OPTIONS.filter(([key]) => key !== "custom") : browserConfig.LOCALE_MODE_OPTIONS;
  const current = (key, fallback) => mode === "create" ? fallback : (fp[key] ?? fallback);
  const runtime = instance?.runtime_locale && ACTIVE_BROWSER_STATUS.includes(instance.status)
    ? `<div class="core-upload-platform">当前生效：${escapeHtml(instance.runtime_locale.timezone || "")}（${escapeHtml(browserConfig.localeSourceLabel(instance.runtime_locale.timezone_source))}） · ${escapeHtml(instance.runtime_locale.language || "")}（${escapeHtml(browserConfig.localeSourceLabel(instance.runtime_locale.language_source))}）</div>`
    : "";
  const custom = keep ? "" : `${browserConfigField("自定义时区", `<input name="timezone" value="${escapeHtml(fp.timezone || "")}" placeholder="IANA 名称，例如 America/New_York">`)}${browserConfigField("自定义语言", `<input name="language" value="${escapeHtml(fp.language || "")}" placeholder="例如 en-US">`)}${browserConfigField("Accept-Language", `<input name="accept_language" value="${escapeHtml(fp.accept_language || "")}" placeholder="留空按语言生成，例如 en-US,en;q=0.9">`)}`;
  return `<section data-config-panel="locale" hidden>${runtime}${browserConfigField("时区来源", `<select name="timezone_mode">${selectOptionsMarkup(modes, current("timezone_mode", "exit_ip"), { keep })}</select>`, "按出口 IP：启动前经该节点代理查询出口所在时区，失败时回退到节点地区。")}${browserConfigField("语言来源", `<select name="language_mode">${selectOptionsMarkup(modes, current("language_mode", "exit_ip"), { keep })}</select>`)}${custom}${browserConfigField("地理位置权限", `<select name="geolocation">${selectOptionsMarkup(browserConfig.GEOLOCATION_OPTIONS, current("geolocation", "block"), { keep })}</select>`, "系统定位基于 Wi-Fi 等信号，不经过代理；建议禁止，避免暴露真实位置。")}</section>`;
}

function renderBrowserExtensionList(selectedIds) {
  const library = appState.browser_extensions || [];
  const selected = new Set(selectedIds || []);
  const known = new Set(library.map((item) => item.id));
  const orphan = [...selected].filter((id) => !known.has(id));
  const rows = [
    ...library.map((item) => `<div class="browser-extension-row"><label class="sync-checkbox"><input type="checkbox" name="extensions" value="${escapeHtml(item.id)}" ${selected.has(item.id) ? "checked" : ""}><span>${escapeHtml(item.name)}${item.version ? ` <small>${escapeHtml(item.version)}</small>` : ""}</span></label><button class="icon-button danger" type="button" data-action="delete-browser-extension" data-id="${escapeHtml(item.id)}" aria-label="从扩展库删除 ${escapeHtml(item.name)}" title="从扩展库删除">×</button></div>`),
    ...orphan.map((id) => `<div class="browser-extension-row"><label class="sync-checkbox"><input type="checkbox" name="extensions" value="${escapeHtml(id)}" checked><span>${escapeHtml(id)} <small>扩展库中已删除，继续使用 Profile 内副本</small></span></label></div>`),
  ];
  return `<div class="browser-extension-list" id="browserExtensionList">${rows.length ? rows.join("") : `<div class="compact-empty">扩展库为空。上传 .zip 或 .crx 扩展包后可在此勾选。</div>`}</div><label class="toolbar-button browser-extension-upload"><input type="file" id="browserExtensionFile" accept=".zip,.crx" hidden><span>上传扩展包</span></label>`;
}

function renderBrowserConfigRuntime(mode, instance) {
  const keep = mode === "batch";
  const cdpControl = remoteBrowserEnabled()
    ? `<div class="core-upload-platform">Docker 远程浏览器暂不支持 CDP 调试端口与 Cookie 导入导出。</div>`
    : keep
    ? browserConfigField("CDP 调试端口", `<select name="cdp_enabled"><option value="${browserConfig.KEEP}" selected>保持不变</option><option value="true">开启</option><option value="false">关闭</option></select>`)
    : `<label class="sync-checkbox"><input type="checkbox" name="cdp_enabled" ${instance?.cdp_enabled ? "checked" : ""}><span>开启 CDP 调试端口（仅 127.0.0.1，随机端口）</span></label><small class="browser-config-note">用于 Cookie 导入导出，也可让 Playwright / Puppeteer 通过 connectOverCDP 接管。端口没有密码，本机任何程序都能控制该浏览器，不用时请关闭。修改后需重启浏览器。</small>`;
  const extensions = keep ? "" : `<fieldset class="browser-config-checks"><legend>扩展</legend>${renderBrowserExtensionList(mode === "create" ? [] : instance?.extensions)}</fieldset>`;
  return `<section data-config-panel="runtime" hidden>${browserConfigField("指纹内核版本", `<select name="kernel_version">${kernelOptionsMarkup(mode === "create" ? "" : instance?.kernel_version || "", { keep })}</select>`, "固定版本可避免默认内核升级后 UA 等特征变化；版本在「内核管理」中安装。")}${cdpControl}${extensions}</section>`;
}

function browserToolsStatus(instance) {
  const running = ACTIVE_BROWSER_STATUS.includes(instance?.status);
  if (remoteBrowserEnabled()) return { cookies: false, reason: "Docker 远程浏览器暂不支持 Cookie 导入导出，请在远程页面中使用浏览器自带功能。" };
  if (!running) return { cookies: false, reason: "启动浏览器后才能导入或导出 Cookie。" };
  if (!instance?.cdp_enabled) return { cookies: false, reason: "请在「内核与扩展」中开启 CDP 调试端口并重启浏览器。" };
  if (!browserConfig.cdpEndpoint(instance)) return { cookies: false, reason: "调试端口正在就绪；若开启 CDP 前已在运行，请重启浏览器。" };
  return { cookies: true, reason: "" };
}

function renderBrowserConfigToolsStatus(instance) {
  const status = browserToolsStatus(instance);
  const endpoint = browserConfig.cdpEndpoint(instance);
  return `${endpoint ? `<div class="browser-config-seed"><span>CDP 地址</span><code title="${escapeHtml(endpoint)}">${escapeHtml(endpoint)}</code><button class="row-button" type="button" data-action="copy" data-text="${escapeHtml(endpoint)}">复制</button></div>` : ""}${status.reason ? `<div class="core-upload-platform">${escapeHtml(status.reason)}</div>` : ""}`;
}

function renderBrowserConfigTools(mode, instance) {
  if (mode !== "edit") return `<section data-config-panel="tools" hidden></section>`;
  const sites = Object.entries(appState.fingerprint_check_sites || {});
  const siteLabels = { browserscan: "BrowserScan", pixelscan: "Pixelscan", creepjs: "CreepJS" };
  const status = browserToolsStatus(instance);
  const disabled = status.cookies ? "" : "disabled";
  return `<section data-config-panel="tools" hidden><fieldset class="browser-config-checks"><legend>指纹检测</legend><div class="browser-config-actions">${sites.map(([key]) => `<button class="row-button" type="button" data-action="open-fingerprint-check" data-site="${escapeHtml(key)}" data-instance="${escapeHtml(instance.id)}">${escapeHtml(siteLabels[key] || key)}</button>`).join("")}</div><small>${remoteBrowserEnabled() ? "远程浏览器请在远程页面中手动打开检测网址。" : "浏览器运行中会新开标签页；未运行时会带检测页启动。"}</small></fieldset><div id="browserConfigToolsStatus">${renderBrowserConfigToolsStatus(instance)}</div><fieldset class="browser-config-checks"><legend>Cookie</legend><div class="browser-config-actions"><button class="row-button" type="button" data-action="export-browser-cookies" data-format="json" data-instance="${escapeHtml(instance.id)}" ${disabled}>导出 JSON</button><button class="row-button" type="button" data-action="export-browser-cookies" data-format="netscape" data-instance="${escapeHtml(instance.id)}" ${disabled}>导出 Netscape</button></div><label class="core-upload-field"><span>导入 Cookie</span><textarea id="browserCookieImport" rows="5" placeholder="粘贴 JSON（Cookie-Editor / EditThisCookie / CDP 格式）或 Netscape cookies.txt" ${disabled}></textarea></label><div class="browser-config-actions"><label class="toolbar-button ${status.cookies ? "" : "is-disabled"}"><input type="file" id="browserCookieFile" accept=".json,.txt" hidden ${disabled}><span>选择文件</span></label><button class="row-button" type="button" data-action="import-browser-cookies" data-instance="${escapeHtml(instance.id)}" ${disabled}>导入到浏览器</button></div><small>导出内容包含登录凭据，请妥善保管，不要发给他人。</small></fieldset></section>`;
}

function setBrowserConfigTab(tab) {
  if (!browserConfigState) return;
  const available = [...document.querySelectorAll("#browserConfigTabs [data-config-tab]")].filter((button) => !button.hidden).map((button) => button.dataset.configTab);
  const next = available.includes(tab) ? tab : available[0] || "basic";
  browserConfigState.tab = next;
  document.querySelectorAll("#browserConfigTabs [data-config-tab]").forEach((button) => {
    const active = button.dataset.configTab === next;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("#browserConfigBody [data-config-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.configPanel !== next;
  });
}

function showBrowserConfigError(message = "") {
  const element = $("#browserConfigError");
  if (!element) return;
  element.textContent = message;
  element.hidden = !message;
}

function openBrowserConfig(mode, { instanceIds = [], nodeId = "", trigger = null, tab = "basic" } = {}) {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  const instance = mode === "edit" ? browserInstanceById(instanceIds[0]) : null;
  if (mode === "edit" && !instance) throw new Error("找不到浏览器记录");
  if ((mode === "batch" || mode === "rebind") && !instanceIds.length) throw new Error("请先选择浏览器记录");
  if ((mode === "create" || mode === "rebind") && !appState.nodes.some((node) => !node.browser_preserved)) {
    throw new Error("没有可用节点，请先导入订阅");
  }
  browserConfigState = { mode, instanceIds: [...instanceIds], nodeId, tab };
  lastBrowserConfigTrigger = trigger;
  const titles = {
    create: "新建浏览器",
    edit: `配置 ${instance?.name || "浏览器"}`,
    batch: `批量配置 ${instanceIds.length} 条记录`,
    rebind: `换绑 ${instanceIds.length} 条记录`,
  };
  $("#browserConfigTitle").textContent = titles[mode];
  $("#browserConfigMeta").textContent = mode === "edit"
    ? `${nodeById(instance.node_id)?.name || "已删除节点"} · ${browserStatusLabel(instance.status)}${ACTIVE_BROWSER_STATUS.includes(instance.status) ? " · 指纹与内核修改在下次启动时生效" : ""}`
    : mode === "create" ? "创建后可在列表中启动；首次启动时创建 Profile。" : "";
  $("#browserConfigSubmit").querySelector("span").textContent = mode === "create" ? "创建" : mode === "rebind" ? "换绑" : "保存";
  document.querySelectorAll("#browserConfigTabs [data-config-tab]").forEach((button) => {
    button.hidden = mode === "rebind" || (button.dataset.configTab === "tools" && mode !== "edit");
  });
  $("#browserConfigTabs").hidden = mode === "rebind";
  $("#browserConfigBody").innerHTML = [
    renderBrowserConfigBasic(mode, instance),
    mode === "rebind" ? "" : renderBrowserConfigFingerprint(mode, instance),
    mode === "rebind" ? "" : renderBrowserConfigLocale(mode, instance),
    mode === "rebind" ? "" : renderBrowserConfigRuntime(mode, instance),
    mode === "rebind" ? "" : renderBrowserConfigTools(mode, instance),
  ].join("");
  showBrowserConfigError();
  setBrowserConfigTab(tab);
  const modal = $("#browserConfigModal");
  const recordsModal = $("#browserRecordModal");
  modal.dataset.returnToBrowserRecords = recordsModal.hidden ? "false" : "true";
  if (!recordsModal.hidden) {
    recordsModal.inert = true;
    recordsModal.setAttribute("aria-hidden", "true");
  }
  openModal(modal);
  requestAnimationFrame(() => modal.querySelector("#browserConfigBody input:not([type=hidden]):not([disabled]), #browserConfigBody select:not([disabled])")?.focus());
}

function closeBrowserConfig() {
  const modal = $("#browserConfigModal");
  if (!modal || modal.hidden) return;
  closeModal(modal);
  if (modal.dataset.returnToBrowserRecords === "true") {
    const recordsModal = $("#browserRecordModal");
    recordsModal.inert = false;
    recordsModal.removeAttribute("aria-hidden");
    if (appState.browserRecordNodeId) renderBrowserRecords(appState.browserRecordNodeId);
  }
  delete modal.dataset.returnToBrowserRecords;
  browserConfigState = null;
  $("#browserConfigBody").innerHTML = "";
  const trigger = lastBrowserConfigTrigger?.isConnected ? lastBrowserConfigTrigger : null;
  trigger?.focus();
  lastBrowserConfigTrigger = null;
}

// 状态轮询只刷新「Cookie 与检测」的运行状态，其余表单草稿保持不变。
function refreshBrowserConfigTools() {
  if (!browserConfigState || browserConfigState.mode !== "edit") return;
  const instance = browserInstanceById(browserConfigState.instanceIds[0]);
  const target = $("#browserConfigToolsStatus");
  if (!instance || !target) return;
  const markup = renderBrowserConfigToolsStatus(instance);
  if (target.innerHTML !== markup) target.innerHTML = markup;
  const enabled = browserToolsStatus(instance).cookies;
  document.querySelectorAll('#browserConfigBody [data-action="export-browser-cookies"], #browserConfigBody [data-action="import-browser-cookies"], #browserCookieImport, #browserCookieFile').forEach((element) => {
    element.disabled = !enabled;
  });
  document.querySelector("#browserCookieFile")?.closest("label")?.classList.toggle("is-disabled", !enabled);
}

function readBrowserConfigForm() {
  const form = $("#browserConfigForm");
  const field = (name) => form.querySelector(`[name="${name}"]`);
  const value = (name) => field(name)?.value;
  const checked = (name) => (field(name) ? field(name).checked : undefined);
  const batch = browserConfigState?.mode === "batch";
  const values = {
    name: value("name"),
    label: value("label"),
    labelEnabled: checked("labelEnabled"),
    group: value("group"),
    groupEnabled: checked("groupEnabled"),
    platform: value("platform"),
    platform_version: value("platform_version"),
    brand: value("brand"),
    brand_version: value("brand_version"),
    hardware_concurrency: value("hardware_concurrency"),
    window_size: value("window_size"),
    timezone_mode: value("timezone_mode"),
    timezone: value("timezone"),
    language_mode: value("language_mode"),
    language: value("language"),
    accept_language: value("accept_language"),
    geolocation: value("geolocation"),
    kernel_version: value("kernel_version"),
    cdp_enabled: batch ? value("cdp_enabled") : checked("cdp_enabled"),
    start_urls: value("start_urls"),
  };
  if (!batch) {
    if (form.querySelector('[name="disable_spoofing"]')) {
      values.disable_spoofing = [...form.querySelectorAll('[name="disable_spoofing"]:checked')].map((input) => input.value);
    }
    if (form.querySelector("#browserExtensionList")) {
      values.extensions = [...form.querySelectorAll('[name="extensions"]:checked')].map((input) => input.value);
    }
  }
  return { values, nodeId: value("node_id") || "", count: Number(value("count") || 1) };
}

async function submitBrowserConfig(event) {
  event.preventDefault();
  if (!browserConfigState) return;
  const submit = $("#browserConfigSubmit");
  if (submit.disabled) return;
  const { mode, instanceIds } = browserConfigState;
  const { values, nodeId, count } = readBrowserConfigForm();
  showBrowserConfigError();
  submit.disabled = true;
  try {
    let message = "";
    if (mode === "rebind") {
      if (!nodeId) throw new Error("请选择目标节点");
      const result = await api("/api/browser/rebind", { instance_ids: instanceIds, node_id: nodeId });
      appState.selectedBrowserIds.clear();
      message = `已换绑 ${result.updated} 条记录到「${nodeById(nodeId)?.name || nodeId}」`;
    } else if (mode === "create") {
      if (!nodeId) throw new Error("请选择绑定节点");
      if (!Number.isInteger(count) || count < 1 || count > 50) throw new Error("新建数量需在 1–50 之间");
      const result = await api("/api/browser/create", { ...browserConfig.buildConfigPayload(values), node_id: nodeId, count });
      message = `已新建 ${result.created} 条浏览器记录`;
    } else if (mode === "batch") {
      const payload = browserConfig.buildConfigPayload(values, { batch: true });
      if (!Object.keys(payload).length) throw new Error("没有选择要修改的项");
      const result = await api("/api/browser/update", { ...payload, instance_ids: instanceIds });
      message = `已更新 ${result.updated} 条记录${result.restart_required ? "，运行中的浏览器重启后生效" : ""}`;
    } else {
      const instance = browserInstanceById(instanceIds[0]);
      const result = await api("/api/browser/update", { ...browserConfig.buildConfigPayload(values), instance_id: instanceIds[0] });
      message = result.restart_required ? "已保存，重启浏览器后生效" : "浏览器配置已保存";
      if (nodeId && instance && nodeId !== instance.node_id) {
        await api("/api/browser/rebind", { instance_ids: instanceIds, node_id: nodeId });
        message = `已保存并换绑到「${nodeById(nodeId)?.name || nodeId}」`;
      }
    }
    await loadStatus({ forceFresh: true });
    closeBrowserConfig();
    showToast(message);
  } catch (error) {
    showBrowserConfigError(error.message);
  } finally {
    submit.disabled = false;
  }
}

async function uploadBrowserExtension(file) {
  if (!file) return;
  if (!/\.(zip|crx)$/i.test(file.name)) throw new Error("扩展包只支持 .zip 或 .crx");
  if (file.size > 64 * 1024 * 1024) throw new Error("扩展包不能超过 64 MiB");
  const headers = {
    "Content-Type": "application/octet-stream",
    "X-ProxyBridge-Upload-Name": encodeURIComponent(file.name),
  };
  if (managementToken) headers[managementAuth.headerName] = managementToken;
  const response = await fetch("/api/browser/extensions/upload", { method: "POST", headers, body: file });
  const responseToken = response.headers.get(managementAuth.headerName) || "";
  if (!managementAuth.validateResponseToken(window, managementToken, responseToken)) {
    managementToken = "";
    throw new Error("管理 API 凭证已失效，请从启动入口重新打开控制台");
  }
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
  appState.browser_extensions = Array.isArray(payload.extensions) ? payload.extensions : appState.browser_extensions;
  refreshBrowserExtensionList(payload.extension?.id);
  showToast(payload.already_installed ? `扩展已在库中：${payload.extension?.name || file.name}` : `已导入扩展：${payload.extension?.name || file.name}`);
}

// 扩展库变化后只重绘扩展列表，保留已勾选状态与其他表单草稿。
function refreshBrowserExtensionList(checkId = "") {
  const list = $("#browserExtensionList");
  if (!list) return;
  const checked = new Set([...list.querySelectorAll('[name="extensions"]:checked')].map((input) => input.value));
  if (checkId) checked.add(checkId);
  const wrapper = list.parentElement;
  const legend = wrapper.querySelector("legend")?.outerHTML || "";
  wrapper.innerHTML = legend + renderBrowserExtensionList([...checked]);
}

async function deleteBrowserExtension(extensionId) {
  const item = (appState.browser_extensions || []).find((entry) => entry.id === extensionId);
  const users = (appState.browser_instances || []).filter((instance) => (instance.extensions || []).includes(extensionId)).length;
  if (!await confirmAction(
    `从扩展库删除「${item?.name || extensionId}」${users ? `，并从 ${users} 条浏览器记录中移除` : ""}。各 Profile 内的扩展副本会在下次启动时清理，扩展数据随之删除。`,
    { title: "删除扩展", confirmLabel: "删除扩展" }
  )) return;
  const result = await api("/api/browser/extensions/delete", { id: extensionId });
  appState.browser_extensions = result.extensions || [];
  const checkbox = document.querySelector(`#browserExtensionList [name="extensions"][value="${browserUi.cssEscape(extensionId, window.CSS)}"]`);
  if (checkbox) checkbox.checked = false;
  refreshBrowserExtensionList();
  await loadStatus({ forceFresh: true });
  showToast("扩展已删除");
}

function downloadTextFile(filename, content, type = "text/plain") {
  const blob = new Blob([content], { type: `${type};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportBrowserCookies(instanceId, format) {
  const result = await api("/api/browser/cookies/export", { instance_id: instanceId, format });
  downloadTextFile(result.filename || browserConfig.cookieFileName(browserInstanceById(instanceId), format), result.content || "", format === "json" ? "application/json" : "text/plain");
  showToast(`已导出 ${result.count} 条 Cookie`);
}

async function importBrowserCookies(instanceId) {
  const textarea = $("#browserCookieImport");
  const content = String(textarea?.value || "").trim();
  if (!content) throw new Error("请粘贴 Cookie 内容或选择文件");
  if (content.length > 900_000) throw new Error("Cookie 内容过大，请拆分后分批导入");
  const result = await api("/api/browser/cookies/import", { instance_id: instanceId, content, format: "auto" });
  if (textarea) textarea.value = "";
  showToast(result.failed ? `已导入 ${result.imported}/${result.total} 条 Cookie，${result.failed} 条被浏览器拒绝` : `已导入 ${result.imported} 条 Cookie`, result.failed ? "error" : "ok");
  if (result.failed) console.warn("Cookie 导入失败项", result.errors);
}

async function openFingerprintCheck(instanceId, site) {
  const instance = browserInstanceById(instanceId);
  if (!instance) throw new Error("找不到浏览器记录");
  if (remoteBrowserEnabled()) {
    const url = appState.fingerprint_check_sites?.[site] || "";
    await copyText(url);
    showToast(`已复制检测网址，请在远程浏览器中打开：${url}`);
    return;
  }
  const result = await api("/api/browser/open-url", { instance_id: instanceId, site });
  await loadStatus({ forceFresh: true });
  showToast(result.method === "launch" ? "已带检测页启动浏览器" : "已在浏览器中打开检测页");
}

async function regenerateBrowserFingerprints(instanceIds) {
  const records = instanceIds.map(browserInstanceById).filter(Boolean);
  if (!records.length) throw new Error("请先选择浏览器记录");
  if (records.some((item) => ACTIVE_BROWSER_STATUS.includes(item.status))) throw new Error("请先停止所选浏览器，再重新生成指纹");
  if (!await confirmAction(
    `将为 ${records.length} 条记录生成新的指纹种子。Canvas、WebGL、Audio 等特征会全部改变，已登录的网站可能把它们识别为新设备并要求重新验证。`,
    { title: "重新生成指纹", confirmLabel: "重新生成" }
  )) return false;
  const result = await api("/api/browser/regenerate-fingerprint", { instance_ids: records.map((item) => item.id) });
  await loadStatus({ forceFresh: true });
  showToast(`已重新生成 ${result.updated} 条记录的指纹`);
  return true;
}

function selectedBrowserRecords() {
  return [...appState.selectedBrowserIds].map(browserInstanceById).filter(Boolean);
}

async function batchLaunchSelectedBrowsers() {
  const records = selectedBrowserRecords().filter((item) => !ACTIVE_BROWSER_STATUS.includes(item.status));
  if (!records.length) return showToast("所选记录都已在运行", "error");
  if (remoteBrowserEnabled()) {
    pendingRemoteBrowserBatchInstances = records.map((item) => ({ nodeId: item.node_id, instanceId: item.id }));
    showRemoteBrowserLaunchModal(records[0].node_id, records[0].id, document.querySelector('[data-action="batch-browser-launch"]'));
    return;
  }
  let started = 0;
  const failed = [];
  for (const record of records) {
    try { await requestBrowserLaunch(record.node_id, record.id); started += 1; }
    catch (error) { failed.push(`${record.name || record.id}: ${error.message}`); }
  }
  await loadStatus({ forceFresh: true });
  showToast(failed.length ? `已启动 ${started} 个浏览器，${failed.length} 个失败：${failed[0]}` : `已启动 ${started} 个浏览器`, failed.length ? "error" : "ok");
}

async function batchStopSelectedBrowsers() {
  const records = selectedBrowserRecords().filter((item) => ACTIVE_BROWSER_STATUS.includes(item.status));
  if (!records.length) return showToast("所选记录中没有运行中的浏览器", "error");
  if (!await confirmAction(`将停止 ${records.length} 个指纹浏览器，Profile 和本地数据会保留。`, { title: "停止所选浏览器", confirmLabel: "停止" })) return;
  for (const record of records) await api("/api/browser/stop", { instance_id: record.id });
  await loadStatus({ forceFresh: true });
  showToast(`已停止 ${records.length} 个浏览器`);
}

async function batchDeleteSelectedBrowsers() {
  const records = selectedBrowserRecords();
  if (!records.length) return;
  const syncEnabled = Boolean(appState.sync?.enabled && appState.sync?.url);
  if (!await confirmAction(
    `将先停止并永久删除 ${records.length} 条浏览器记录及其 Profile、Cookie 和本地存储；此操作无法恢复。${syncEnabled ? " WebDAV 已启用，远端对应数据也会同步删除。" : ""}`,
    { title: "永久删除浏览器 Profile", confirmLabel: "永久删除", details: records.slice(0, 5).map((item) => item.label || item.name).join("、") + (records.length > 5 ? ` 等 ${records.length} 条` : "") }
  )) return;
  const failed = [];
  for (const record of records) {
    try { await api("/api/browser/delete", { instance_id: record.id }); appState.selectedBrowserIds.delete(record.id); }
    catch (error) { failed.push(`${record.name || record.id}: ${error.message}`); }
  }
  await loadStatus({ forceFresh: true });
  showToast(failed.length ? `已删除 ${records.length - failed.length} 条，${failed.length} 条失败：${failed[0]}` : `已删除 ${records.length} 条浏览器记录`, failed.length ? "error" : "ok");
}

// ============ 指纹内核版本 ============
async function setDefaultFingerprintKernel(kernelId) {
  await api("/api/browser/kernels/default", { id: kernelId });
  await loadStatus({ forceFresh: true });
  await loadCoreCatalog();
  showToast("默认指纹内核已更新，新启动的浏览器将使用该版本");
}

async function deleteFingerprintKernel(kernelId) {
  const kernel = (appState.fingerprint_kernels || []).find((item) => item.id === kernelId);
  if (!await confirmAction(
    `删除指纹内核「${kernel?.version || kernel?.label || kernelId}」会移除其安装文件；需要先停止所有浏览器。浏览器 Profile 不受影响。`,
    { title: "删除指纹内核版本", confirmLabel: "删除" }
  )) return;
  await api("/api/browser/kernels/delete", { id: kernelId });
  await loadStatus({ forceFresh: true });
  await loadCoreCatalog();
  showToast("指纹内核版本已删除");
}

async function installFingerprintKernel(payload, label) {
  if (kernelDownloadPromise) return kernelDownloadPromise;
  showToast(`正在下载指纹内核${label ? ` ${label}` : ""}，约 150-250MB，请耐心等待…`);
  kernelDownloadPromise = (async () => {
    const result = await api("/api/browser/download-kernel", payload);
    await loadStatus({ forceFresh: true });
    await loadCoreCatalog();
    if (result.already_installed) showToast(`该版本已安装${result.kernel_id ? `：${result.kernel_id}` : ""}`);
    else if (result.arch_warning) showToast(result.arch_warning, "error");
    else showToast(`指纹内核安装完成：${result.kernel_version || result.kernel_id || ""}`);
    return result;
  })().finally(() => {
    kernelDownloadPromise = null;
  });
  return kernelDownloadPromise;
}

async function openKernelReleases(trigger = null) {
  const modal = $("#kernelReleaseModal");
  const body = $("#kernelReleaseBody");
  modal.dataset.trigger = trigger ? actionLockKey(trigger) : "";
  body.innerHTML = `<div class="compact-empty">正在获取上游版本列表…</div>`;
  $("#kernelReleaseMeta").textContent = "";
  openModal(modal);
  modal.querySelector('[data-action="close-kernel-releases"]')?.focus();
  try {
    const result = await api("/api/browser/kernels/releases", {});
    $("#kernelReleaseMeta").textContent = `${result.os}/${result.arch} · 来自 adryfish/fingerprint-chromium`;
    const releases = Array.isArray(result.releases) ? result.releases : [];
    body.innerHTML = releases.length
      ? `<div class="responsive-table"><table><thead><tr><th>版本</th><th>发布时间</th><th>操作</th></tr></thead><tbody>${releases.map((release) => `<tr><td><strong>${escapeHtml(release.kernel_id)}</strong><small title="${escapeHtml(release.asset)}">${escapeHtml(release.asset)}</small></td><td>${escapeHtml(String(release.published_at || "").slice(0, 10) || "—")}</td><td>${release.installed ? `<span class="status-pill is-ok">已安装</span>` : `<button class="row-button" data-action="install-kernel-release" data-tag="${escapeHtml(release.tag)}" data-id="${escapeHtml(release.kernel_id)}">下载安装</button>`}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="compact-empty">上游暂未发布适配本机的版本，可稍后重试或使用「本地导入」。</div>`;
  } catch (error) {
    body.innerHTML = `<div class="core-upload-error" role="alert">${escapeHtml(error.message)}</div>`;
  }
}

function closeKernelReleases() {
  const modal = $("#kernelReleaseModal");
  if (!modal || modal.hidden) return;
  closeModal(modal);
  findActionTrigger(modal.dataset.trigger || "")?.focus();
  delete modal.dataset.trigger;
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

async function copyText(text, { sensitive = false } = {}) {
  let copied = false;
  try {
    const result = await api("/api/clipboard", { text });
    copied = result.ok === true;
  } catch {}

  if (!copied) {
    copied = await browserUi.copyText(text, window);
  }

  if (!copied) throw new Error("当前环境无法复制到剪贴板");

  const isSensitive = sensitive || /\/pool\/[^/]+|[?&](token|key|auth|secret)=/i.test(String(text));
  showToast(isSensitive ? "敏感链接已复制，请谨慎分享。" : `已复制 ${text}`);
}

function bridgeCopyValue(nodeId, format) {
  const bridge = bridgeForNode(nodeId);
  if (!bridge) throw new Error("该节点当前没有运行中的代理");
  const socks = bridge.socks_url || `socks5h://127.0.0.1:${bridge.port}`;
  const http = bridge.http_url || `http://127.0.0.1:${bridge.port}`;
  const endpoint = endpointParts(socks, bridge.port);
  if (format === "env") return `export ALL_PROXY='${socks}'\nexport HTTP_PROXY='${http}'\nexport HTTPS_PROXY='${http}'`;
  if (format === "curl") return `curl --proxy '${http}' https://example.com/`;
  const name = String(bridge.node_name || "Proxy").replaceAll("'", "''");
  return `proxies:\n  - name: '${name}'\n    type: socks5\n    server: 127.0.0.1\n    port: ${Number(endpoint.port)}`;
}

async function copyBridgeFormat(nodeId, format) {
  await copyText(bridgeCopyValue(nodeId, format));
}

async function batchTestNodes() {
  const ids = [...appState.selectedNodeIds];
  if (!ids.length) return showToast("请先选择节点", "error");
  if (ids.length > 500) return showToast("一次最多测速 500 个节点，请缩小选择范围", "error");
  const task = await api("/api/nodes/test-batch/start", { node_ids: ids });
  appState.speedTest.tasks = [...(appState.speedTest.tasks || []), task];
  renderActiveView({ force: true });
  showToast(`已开始测试 ${ids.length} 个节点`);
}

function nodeHasFreshLatency(node) {
  const result = appState.tests[node.id];
  return Boolean(result?.ok && !result.stale && Number.isFinite(result.latency_ms) && result.latency_ms >= 0);
}

async function startFastestNodes(nodes, count) {
  const requestedCount = Number(count);
  if (!Number.isInteger(requestedCount) || requestedCount < 1) throw new Error("启动数量必须是正整数");
  const candidates = nodes
    .filter((node) => !bridgeForNode(node.id) && nodeHasFreshLatency(node))
    .sort((left, right) => Number(appState.tests[left.id].latency_ms) - Number(appState.tests[right.id].latency_ms))
    .slice(0, requestedCount);
  if (!candidates.length) {
    showToast("没有未运行且测速成功、结果未过期的节点。", "error");
    return;
  }
  let started = 0;
  const errors = [];
  for (const node of candidates) {
    try {
      await api("/api/bridges/start", { node_id: node.id, core: "auto" });
      started += 1;
    } catch (error) {
      errors.push(`${node.name}: ${error.message}`);
    }
  }
  await loadStatus({ forceFresh: true });
  showToast(errors.length ? `已启动 ${started} 个节点，${errors.length} 个失败` : `已启动延迟最低的 ${started} 个节点`);
  if (errors.length) console.warn("启动最快节点时发生错误", errors);
}

async function batchStopNodes() {
  const bridges = [...appState.selectedNodeIds].map(bridgeForNode).filter(Boolean);
  if (!bridges.length) return showToast("所选节点没有运行中的代理");
  if (!await confirmAction(`将停止 ${bridges.length} 个代理，相关连接会中断，关联浏览器会关闭；浏览器 Profile、Cookie 和登录态会保留。`, { title: "批量停止代理", confirmLabel: "批量停止" })) return;
  for (const bridge of bridges) await api("/api/bridges/stop", { id: bridge.id });
  await loadStatus({ forceFresh: true });
  clearNodeSelections();
  showToast(`已停止 ${bridges.length} 个代理`);
}

async function batchLaunchBrowsers() {
  const ids = [...appState.selectedNodeIds];
  if (!browserEnabled()) throw new Error("当前运行模式未启用指纹浏览器");
  if (!ids.length) return showToast("请先选择节点", "error");
  if (remoteBrowserEnabled()) {
    pendingRemoteBrowserBatchNodeIds = ids;
    showRemoteBrowserLaunchModal(ids[0], "", document.querySelector('[data-action="batch-launch-browsers"]'));
    return;
  }
  let started = 0;
  const failed = [];
  for (const nodeId of ids) {
    try { await requestBrowserLaunch(nodeId); started += 1; }
    catch (error) { failed.push(`${nodeById(nodeId)?.name || nodeId}: ${error.message}`); }
  }
  await loadStatus({ forceFresh: true });
  clearNodeSelections();
  showToast(failed.length ? `已启动 ${started} 个浏览器，${failed.length} 个失败` : `已启动 ${started} 个浏览器`);
  if (failed.length) console.warn("批量启动浏览器失败", failed);
}

async function batchExcludePoolMembers() {
  const selected = new Set(appState.selectedNodeIds);
  const members = (appState.pool?.members || []).filter((member) => selected.has(member.node_id));
  if (!members.length) return showToast("所选节点中没有代理池成员", "error");
  for (const member of members) await api("/api/pool/exclude", { id: member.id, duration_seconds: 1800 });
  await loadStatus({ forceFresh: true });
  clearNodeSelections();
  showToast(`已将 ${members.length} 个节点暂时移出代理池`);
}

async function stopAllVisibleBrowsers() {
  const records = browserConfig.filterBrowserRecords(
    browserUi.sortBrowserInstancesByRecentUse(appState.browser_instances || []),
    browserPageFilter,
    nodeById,
  ).filter((item) => browserConfig.isActive(item));
  if (!records.length) return;
  if (!await confirmAction(`将停止 ${records.length} 个指纹浏览器，Profile 和本地数据会保留。`, { title: "停止指纹浏览器", confirmLabel: "全部停止" })) return;
  for (const record of records) await api("/api/browser/stop", { instance_id: record.id });
  await loadStatus({ forceFresh: true });
  showToast(`已停止 ${records.length} 个浏览器`);
}

async function saveAllGeneralSettings(trigger) {
  const submitEvent = { preventDefault() {} };
  if (networkFormDirty) await withNetworkSettingsSave(() => saveNetworkSettings(submitEvent), trigger);
  if (speedTestFormDirty) await saveSpeedTestSettings(submitEvent);
  if (logSettingsDirty) await saveLogSettings(submitEvent);
  updateSettingsSaveBar();
}

function discardGeneralSettings() {
  networkFormDirty = false;
  speedTestFormDirty = false;
  logSettingsDirty = false;
  if (appState.network) applyNetworkSettingsToForm(appState.network, { force: true });
  renderNetworkSettings();
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
  const isRemoteBatchLaunch = pendingRemoteBrowserBatchNodeIds.length > 0;
  const isRemoteRecordBatch = pendingRemoteBrowserBatchInstances.length > 0;
  const batchTargets = isRemoteRecordBatch
    ? pendingRemoteBrowserBatchInstances.map((item) => ({ ...item }))
    : pendingRemoteBrowserBatchNodeIds.length
    ? pendingRemoteBrowserBatchNodeIds.map((nodeId) => ({ nodeId, instanceId: "" }))
    : [{ nodeId: pending.nodeId, instanceId: pending.instanceId }];
  remoteBrowserLaunchPromise = (async () => {
    let lastResult = null;
    const failures = [];
    for (const target of batchTargets) {
      try {
        lastResult = await requestBrowserLaunch(target.nodeId, target.instanceId, { username, password });
      } catch (error) {
        const label = target.instanceId ? browserInstanceById(target.instanceId)?.name : nodeById(target.nodeId)?.name;
        failures.push(`${label || target.instanceId || target.nodeId}: ${error.message}`);
      }
    }
    if (!lastResult) throw new Error(failures.join("；") || "没有成功启动远程浏览器");
    lastResult.batch_count = batchTargets.length - failures.length;
    lastResult.batch_failures = failures;
    return lastResult;
  })();
  try {
    result = await remoteBrowserLaunchPromise;
    const remoteUrl = safeRemoteBrowserUrl(result.remote_url);
    if (!remoteUrl) throw new Error("浏览器 Worker 未返回有效的远程访问地址");
    showRemoteBrowserLaunchSuccess(result, { username, password });
    pendingRemoteBrowserLaunch = null;
    pendingRemoteBrowserBatchNodeIds = [];
    pendingRemoteBrowserBatchInstances = [];
    if (isRemoteBatchLaunch) clearNodeSelections();
    if (isRemoteRecordBatch) appState.selectedBrowserIds.clear();
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
    showToast(result.batch_count > 1
      ? `已启动 ${result.batch_count} 个远程浏览器${result.batch_failures?.length ? `，${result.batch_failures.length} 个失败` : ""}；${popupHint}`
      : `${browserName} 已启动，${popupHint}`);
    if (result.batch_failures?.length) console.warn("批量启动远程浏览器失败", result.batch_failures);
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
    "open-browser-create",
    "open-browser-config",
    "open-browser-batch-edit",
    "open-browser-rebind",
    "close-browser-config",
    "clear-browser-selection",
    "close-kernel-releases",
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
    if (action === "close-batch-start-results") {
      closeBatchStartResults();
    } else if (action === "command-run") {
      closeCommandPalette();
      if (button.dataset.command === "page") setActiveView(button.dataset.view);
      else {
        const bridge = bridgeForNode(button.dataset.node);
        if (bridge) await copyText(bridge.socks_url);
        else await startNode(button.dataset.node);
      }
    } else if (action === "accept-confirm") {
      finishConfirmation(true);
    } else if (action === "cancel-confirm") {
      finishConfirmation(false);
    } else if (action === "navigate") {
      setActiveView(button.dataset.view || "overview");
    } else if (action === "open-subscription-drawer") {
      setActiveView("subscriptions");
      openDrawer($("#subscriptionDrawer"), '[name="url"]');
    } else if (action === "close-subscription-drawer") {
      closeDrawer($("#subscriptionDrawer"));
    } else if (action === "preview-subscription-filter") {
      await previewSubscriptionImport();
    } else if (action === "open-node-detail") {
      openNodeDetail(button.dataset.node);
    } else if (action === "close-node-drawer") {
      closeDrawer($("#nodeDetailDrawer"));
    } else if (action === "toggle-node-favorite") {
      const node = nodeById(button.dataset.node);
      if (node) {
        const result = await api("/api/nodes/update", { node_id: node.id, favorite: !node.favorite });
        Object.assign(node, result.node || { favorite: !node.favorite });
        renderNodes();
        showToast(node.favorite ? "已收藏并置顶" : "已取消收藏");
      }
    } else if (action === "save-node-details") {
      const node = nodeById(button.dataset.node);
      if (!node) throw new Error("找不到节点");
      const preferredCore = $("[data-core-select]")?.value || node.preferred_core;
      const fallbackEnabled = Boolean($("[data-core-fallback]")?.checked);
      const changedCore = preferredCore !== node.preferred_core || fallbackEnabled !== Boolean(node.fallback_enabled);
      const result = await api("/api/nodes/update", {
        node_id: node.id,
        region: $("#nodeDetailRegion").value,
        favorite: Boolean($("#nodeDetailFavorite").checked),
        remark: $("#nodeDetailRemark").value.trim(),
      });
      Object.assign(node, result.node || {});
      if (changedCore) {
        const coreResult = await api("/api/nodes/core", { node_id: node.id, preferred_core: preferredCore, fallback_enabled: fallbackEnabled });
        Object.assign(node, coreResult.node || {});
      }
      renderNodeDetailDrawer();
      renderNodes();
      showToast("节点详情已保存");
    } else if (action === "launch-browser-from-drawer") {
      await launchBrowser(currentNodeDetailId, "", button);
    } else if (action === "toggle-pool-links") {
      poolLinksExpanded = !poolLinksExpanded;
      renderPool();
    } else if (action === "select-pool-format") {
      currentPoolLinkFormat = ["list_json", "proxies_txt", "get_health"].includes(button.dataset.poolFormat)
        ? button.dataset.poolFormat
        : "list_json";
      renderPool();
    } else if (action === "reveal-pool-link") {
      const key = `pool:${currentPoolLinkFormat}`;
      if (revealedSecrets.has(key)) revealedSecrets.delete(key); else revealedSecrets.add(key);
      renderPool();
    } else if (action === "copy-pool-link") {
      const links = appState.pool?.links || {};
      const url = currentPoolLinkFormat === "get_health"
        ? links.get_health || links.get_random
        : links[currentPoolLinkFormat];
      if (url) await copyText(url, { sensitive: true });
    } else if (action === "reveal-subscription-url") {
      const key = `subscription:${button.dataset.id}`;
      if (revealedSecrets.has(key)) revealedSecrets.delete(key); else revealedSecrets.add(key);
      renderSubscriptions();
    } else if (action === "copy-subscription-url") {
      const item = appState.subscriptions.find((subscription) => subscription.id === button.dataset.id);
      if (item?.url) await copyText(item.url, { sensitive: true });
    } else if (action === "save-subscription-filters") {
      await saveSubscriptionFilters(button.dataset.id, button);
    } else if (action === "batch-test-nodes") {
      await batchTestNodes();
    } else if (action === "batch-start-nodes") {
      await batchStartNodes();
    } else if (action === "batch-stop-nodes") {
      await batchStopNodes();
    } else if (action === "batch-launch-browsers") {
      await batchLaunchBrowsers();
    } else if (action === "batch-exclude-pool") {
      await batchExcludePoolMembers();
    } else if (action === "stop-all-browsers") {
      await stopAllVisibleBrowsers();
    } else if (action === "save-all-settings") {
      await saveAllGeneralSettings(button);
    } else if (action === "discard-settings") {
      discardGeneralSettings();
    } else if (action === "reveal-lan-token") {
      const input = $("#lanToken");
      input.type = input.type === "password" ? "text" : "password";
    } else if (action === "copy-lan-token") {
      if ($("#lanToken").value) await copyText($("#lanToken").value, { sensitive: true });
    } else if (action === "open-system-log-search") {
      appState.systemLogFilters.search = button.dataset.search || "";
      appState.systemLogPage.page = 1;
      setActiveView("system");
      await loadLogPage("system");
    } else if (action === "test-node-group") {
      const group = groupNodesBySubscription(filteredNodes()).find((item) => item.id === button.dataset.group);
      if (group?.nodes.length) {
        const task = await api("/api/nodes/test-batch/start", { node_ids: group.nodes.map((node) => node.id) });
        appState.speedTest.tasks = [...(appState.speedTest.tasks || []), task];
        showToast(`已启动 ${group.nodes.length} 个节点的测速`);
      }
    } else if (action === "start-fastest-group") {
      const group = groupNodesBySubscription(filteredNodes()).find((item) => item.id === button.dataset.group);
      await startFastestNodes(group?.nodes || [], 1);
    } else if (action === "start-fastest-visible") {
      await startFastestNodes(filteredNodes(), Number($("#fastestNodeCount").value || 1));
    } else if (action === "copy-bridge-format") {
      await copyBridgeFormat(button.dataset.node, button.dataset.format);
    } else if (action === "toggle-node-group") {
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
      if (!await confirmAction("将关闭此节点关联的指纹浏览器进程，现有标签页会结束；Profile、Cookie 和登录态会保留。", { title: "停止节点浏览器", confirmLabel: "停止浏览器" })) return;
      await stopBrowser(button.dataset.node);
    } else if (action === "stop-browser-instance") {
      if (!await confirmAction("将关闭此浏览器进程，现有标签页会结束；Profile、Cookie 和登录态会保留。", { title: "停止浏览器", confirmLabel: "停止浏览器" })) return;
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
    } else if (action === "open-browser-create") {
      openBrowserConfig("create", { nodeId: button.dataset.node || (browserPageFilter.node !== "all" ? browserPageFilter.node : ""), trigger: button });
    } else if (action === "open-browser-config") {
      openBrowserConfig("edit", { instanceIds: [button.dataset.instance], trigger: button, tab: button.dataset.tab || "basic" });
    } else if (action === "open-browser-batch-edit") {
      openBrowserConfig("batch", { instanceIds: [...appState.selectedBrowserIds], trigger: button });
    } else if (action === "open-browser-rebind") {
      openBrowserConfig("rebind", { instanceIds: button.dataset.instance ? [button.dataset.instance] : [...appState.selectedBrowserIds], trigger: button });
    } else if (action === "close-browser-config") {
      closeBrowserConfig();
    } else if (action === "clear-browser-selection") {
      appState.selectedBrowserIds.clear();
      renderBrowserPage();
    } else if (action === "batch-browser-launch") {
      await batchLaunchSelectedBrowsers();
    } else if (action === "batch-browser-stop") {
      await batchStopSelectedBrowsers();
    } else if (action === "batch-browser-regenerate") {
      await regenerateBrowserFingerprints([...appState.selectedBrowserIds]);
    } else if (action === "batch-browser-delete") {
      await batchDeleteSelectedBrowsers();
    } else if (action === "regenerate-browser-fingerprint") {
      if (await regenerateBrowserFingerprints([button.dataset.instance])) {
        const instance = browserInstanceById(button.dataset.instance);
        const seed = button.closest(".browser-config-seed")?.querySelector("code");
        if (seed && instance) seed.textContent = String(instance.fingerprint_seed || "-");
      }
    } else if (action === "open-fingerprint-check") {
      await openFingerprintCheck(button.dataset.instance, button.dataset.site);
    } else if (action === "export-browser-cookies") {
      await exportBrowserCookies(button.dataset.instance, button.dataset.format);
    } else if (action === "import-browser-cookies") {
      await importBrowserCookies(button.dataset.instance);
    } else if (action === "delete-browser-extension") {
      await deleteBrowserExtension(button.dataset.id);
    } else if (action === "set-default-kernel") {
      await setDefaultFingerprintKernel(button.dataset.id);
    } else if (action === "delete-fingerprint-kernel") {
      await deleteFingerprintKernel(button.dataset.id);
    } else if (action === "download-latest-kernel") {
      await installFingerprintKernel({ latest: true }, "最新版本");
    } else if (action === "open-kernel-releases") {
      await openKernelReleases(button);
    } else if (action === "close-kernel-releases") {
      closeKernelReleases();
    } else if (action === "install-kernel-release") {
      await installFingerprintKernel({ tag: button.dataset.tag }, button.dataset.id);
      if (!$("#kernelReleaseModal").hidden) await openKernelReleases();
    } else if (action === "download-kernel") {
      await downloadKernel();
    } else if (action === "update-core") {
      const result = await api("/api/cores/update", { core: button.dataset.core });
      await loadStatus({ forceFresh: true });
      await loadCoreCatalog();
      showToast(`${button.dataset.core} 已更新${result.version ? `：${result.version}` : ""}`);
    } else if (action === "show-core-upload") {
      showCoreUploadModal(button);
    } else if (action === "copy") {
      await copyText(button.dataset.text);
    } else if (action === "rotate-pool-token") {
      if (!await confirmAction("轮换代理池密钥后，所有旧链接会立即失效。", { title: "轮换代理池密钥", confirmLabel: "轮换密钥" })) return;
      await api("/api/pool/token/rotate", {});
      await loadStatus({ forceFresh: true });
      showToast("代理池密钥已轮换，旧链接立即失效");
    } else if (action === "exclude-pool-member") {
      const duration = Number(button.dataset.duration || 0);
      await api("/api/pool/exclude", { id: button.dataset.id, duration_seconds: duration });
      await loadStatus({ forceFresh: true });
      showToast(duration ? "节点已暂时排除 30 分钟" : "节点已恢复到代理池");
    } else if (action === "kick-client") {
      if (!await confirmAction("断开此客户端的代理连接。客户端可以重新连接。", { title: "断开客户端", confirmLabel: "断开" })) return;
      await api("/api/network/clients/kick", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast("客户端连接已断开");
    } else if (action === "stop") {
      const bridge = appState.bridges.find((item) => item.id === button.dataset.id);
      if (!await confirmAction(`将停止「${bridge?.node_name || "本地代理"}」，现有连接会中断${appState.browser_instances.some((item) => item.node_id === bridge?.node_id && ["running", "starting", "stopping"].includes(item.status)) ? "，关联浏览器也会关闭" : ""}。浏览器 Profile、Cookie 和登录态会保留。`, { title: "停止本地代理", confirmLabel: "停止代理" })) return;
      const result = await api("/api/bridges/stop", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast(result.killed_browsers ? "已停止端口，关联浏览器已关闭" : "已停止端口");
    } else if (action === "stop-all") {
      const runningCount = runningBridges().length;
      if (!await confirmAction(`将停止全部 ${runningCount} 个本地代理${appState.smartGateway?.running ? "并停止智能网关" : ""}。现有连接会中断；关联浏览器将关闭，但其 Profile、Cookie 和登录态会保留。`, { title: "停止所有代理", confirmLabel: "全部停止" })) return;
      const result = await api("/api/bridges/stop-all", {});
      await loadStatus({ forceFresh: true });
      showToast(result.killed_browsers ? "已停止全部代理，关联浏览器已关闭" : "已停止全部代理");
    } else if (action === "restart") {
      const bridge = appState.bridges.find((item) => item.id === button.dataset.id);
      if (!await confirmAction(`重启「${bridge?.node_name || "本地代理"}」会中断现有连接并关闭关联浏览器；Profile、Cookie 和登录态会保留。`, { title: "重启本地代理", confirmLabel: "重启" })) return;
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
      if (!await confirmAction("将停止智能网关入口，正在使用该入口的连接会中断。代理池成员和浏览器 Profile 不会删除。", { title: "停止智能网关", confirmLabel: "停止网关" })) return;
      await api("/api/smart-gateway/stop", {});
      await loadStatus({ forceFresh: true });
      showToast("智能网关已停止");
    } else if (action === "delete-sub") {
      const subscriptionId = button.dataset.id;
      const subscription = appState.subscriptions.find((item) => item.id === subscriptionId);
      button.closest(".row-more")?.removeAttribute("open");
      if (!subscription) {
        await loadStatus({ forceFresh: true });
        showToast("订阅列表已更新，请重新操作");
        return;
      }
      const name = subscription.name || "该订阅";
      const nodeCount = Number(subscription.node_count || (subscription.node_ids || []).length || 0);
      const affectedBrowsers = browserEnabled()
        ? (appState.browser_instances || []).filter((item) => (subscription.node_ids || []).includes(item.node_id)).length
        : 0;
      if (!await confirmAction(
        affectedBrowsers
          ? `删除「${name}」将清理最多 ${nodeCount} 个未被其他订阅引用的节点。其中 ${affectedBrowsers} 条浏览器记录默认保留，原节点转为「已移除」状态，可稍后换绑到其他节点。`
          : `删除「${name}」将清理最多 ${nodeCount} 个未被其他订阅引用的节点。`,
        {
          title: "删除订阅",
          confirmLabel: "删除订阅",
          option: affectedBrowsers ? { label: `同时永久删除这 ${affectedBrowsers} 条浏览器记录及 Profile（Cookie、登录态无法恢复）`, checked: false } : null,
        }
      )) return;
      const deleteBrowserProfiles = confirmOptionChecked;
      try {
        await api("/api/subscriptions/delete", { id: subscriptionId, delete_browser_profiles: deleteBrowserProfiles });
      } catch (error) {
        try {
          await loadStatus({ forceFresh: true });
        } catch (_) {
          throw error;
        }
        if (appState.subscriptions.some((item) => item.id === subscriptionId)) throw error;
        showToast("订阅已不存在，列表已更新");
        return;
      }
      await loadStatus({ forceFresh: true });
      showToast("订阅已删除");
    } else if (action === "clear-logs") {
      if (!await confirmAction("清空后将删除当前保存的全部连接日志。此操作不可恢复。", { title: "清空连接日志", confirmLabel: "清空日志" })) return;
      await api("/api/logs/clear", {});
      await loadStatus({ forceFresh: true });
      appState.requestLogPage.page = 1;
      await loadLogPage("request");
      showToast("连接日志已清空");
    } else if (action === "clear-system-logs") {
      if (!await confirmAction("清空后将删除当前保存的全部系统日志。此操作不可恢复。", { title: "清空系统日志", confirmLabel: "清空日志" })) return;
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
    }
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    actionInFlight.delete(lockKey);
    applyActionInFlightState();
    i18n.apply(document);
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
  const filterInclude = String(formData.get("filter_include") || "").trim();
  const filterExclude = String(formData.get("filter_exclude") || "").trim();
  const filterRegex = formData.get("filter_regex") === "on";
  const previewSignature = subscriptionFormPreviewSignature(formData);
  if ((filterInclude || filterExclude) && (!subscriptionPreviewResult || subscriptionPreviewSignature !== previewSignature)) {
    showToast("保存过滤规则前，请先预览命中的节点。", "error");
    return;
  }
  submit.disabled = true;
  try {
    const result = await api("/api/subscriptions/import", {
      name: formData.get("name"),
      url: formData.get("url"),
      raw: formData.get("raw"),
      filter_include: filterInclude,
      filter_exclude: filterExclude,
      filter_regex: filterRegex,
    });
    form.reset();
    form.querySelector(".raw-import-details")?.removeAttribute("open");
    await loadStatus({ forceFresh: true });
    showToast(`导入 ${result.subscription.node_count} 个节点`);
    closeDrawer($("#subscriptionDrawer"));
    subscriptionPreviewResult = null;
    subscriptionPreviewSignature = "";
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

function subscriptionFormPreviewSignature(formData = new FormData($("#importForm"))) {
  return JSON.stringify({
    url: String(formData.get("url") || "").trim(),
    raw: String(formData.get("raw") || "").trim(),
    filter_include: String(formData.get("filter_include") || "").trim(),
    filter_exclude: String(formData.get("filter_exclude") || "").trim(),
    filter_regex: formData.get("filter_regex") === "on",
  });
}

async function previewSubscriptionImport() {
  const form = $("#importForm");
  const data = new FormData(form);
  const signature = subscriptionFormPreviewSignature(data);
  const url = String(data.get("url") || "").trim();
  const raw = String(data.get("raw") || "").trim();
  if (!url && !raw) throw new Error("请先填写订阅 URL 或原始内容");
  const result = await api("/api/subscriptions/preview", {
    url,
    raw,
    filter_include: String(data.get("filter_include") || "").trim(),
    filter_exclude: String(data.get("filter_exclude") || "").trim(),
    filter_regex: data.get("filter_regex") === "on",
  });
  subscriptionPreviewResult = result;
  subscriptionPreviewSignature = signature;
  const output = $("#subscriptionFilterPreview");
  const nodes = Array.isArray(result.nodes) ? result.nodes : [];
  output.innerHTML = `<strong>命中 ${Number(result.matched || 0)} / ${Number(result.total || 0)} 个</strong>${Number(result.excluded || 0) ? ` · 排除 ${Number(result.excluded)} 个` : ""}${nodes.length ? `<ul>${nodes.slice(0, 8).map((node) => `<li>${escapeHtml(node.name || "未命名节点")}</li>`).join("")}${nodes.length > 8 ? `<li>还有 ${nodes.length - 8} 个</li>` : ""}</ul>` : ""}`;
  return result;
}

async function saveSubscriptionFilters(subscriptionId, trigger) {
  const card = trigger.closest(".subscription-item");
  const include = card.querySelector('[data-sub-filter="include"]')?.value || "";
  const exclude = card.querySelector('[data-sub-filter="exclude"]')?.value || "";
  const regex = Boolean(card.querySelector('[data-sub-filter="regex"]')?.checked);
  await api("/api/subscriptions/update", {
    id: subscriptionId,
    filter_include: include,
    filter_exclude: exclude,
    filter_regex: regex,
  });
  await loadStatus({ forceFresh: true });
  showToast("订阅过滤规则已保存，后续刷新将按此规则解析");
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
  const modal = !$("#confirmDialog").hidden
    ? $("#confirmDialog")
    : !$("#batchStartResultDialog").hidden
      ? $("#batchStartResultDialog")
      : !$("#nodeDetailDrawer").hidden
      ? $("#nodeDetailDrawer")
      : !$("#subscriptionDrawer").hidden
        ? $("#subscriptionDrawer")
        : !$("#remoteBrowserCredentialsModal").hidden
    ? $("#remoteBrowserCredentialsModal")
    : !$("#remoteBrowserLaunchModal").hidden
      ? $("#remoteBrowserLaunchModal")
      : !$("#coreUploadModal").hidden
        ? $("#coreUploadModal")
        : !$("#browserConfigModal").hidden
        ? $("#browserConfigModal")
        : !$("#kernelReleaseModal").hidden
        ? $("#kernelReleaseModal")
        : !$("#browserRecordModal").hidden
          ? $("#browserRecordModal")
          : !$("#coreLogModal").hidden
            ? $("#coreLogModal")
            : null;
  if (!modal) return false;
  const dialog = modal.querySelector('[role="dialog"], [role="alertdialog"]') || modal;
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

function scheduleStatusPoll(delay = document.hidden ? STATUS_POLL_HIDDEN_MS : statusEventConnected ? STATUS_POLL_HIDDEN_MS : STATUS_POLL_VISIBLE_MS) {
  const nextDelay = Math.max(0, Number(delay) || 0);
  const deadline = Date.now() + nextDelay;
  if (statusPollTimer && statusPollDeadline <= deadline) return;
  window.clearTimeout(statusPollTimer);
  statusPollDeadline = deadline;
  statusPollTimer = window.setTimeout(runStatusPoll, nextDelay);
}

async function runStatusPoll({ reportError = false } = {}) {
  window.clearTimeout(statusPollTimer);
  statusPollTimer = null;
  statusPollDeadline = 0;
  try {
    await loadStatus();
  } catch (error) {
    if (reportError) showToast(error.message, "error");
  } finally {
    scheduleStatusPoll();
  }
}

async function startStatusEvents() {
  if (statusEventController) statusEventController.abort();
  const controller = new AbortController();
  statusEventController = controller;
  while (!controller.signal.aborted) {
    try {
      const headers = { Accept: "text/event-stream" };
      if (managementToken) headers[managementAuth.headerName] = managementToken;
      const response = await fetch("/api/events", { method: "GET", headers, cache: "no-store", signal: controller.signal });
      if (!response.ok || !response.body) throw new Error(`事件流不可用：HTTP ${response.status}`);
      const responseToken = response.headers.get(managementAuth.headerName);
      if (!managementAuth.validateResponseToken(window, managementToken, responseToken)) throw new Error("管理令牌已失效");
      statusEventConnected = true;
      scheduleStatusPoll(STATUS_POLL_HIDDEN_MS);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (!controller.signal.aborted) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          let eventName = "message";
          const data = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) eventName = line.slice(6).trim();
            else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
          }
          if (eventName === "status" && data.length) {
            try { applyStatusSnapshot(JSON.parse(data.join("\n"))); }
            catch (error) { console.warn("忽略格式错误的状态推送", error); }
          }
        }
      }
      await reader.cancel().catch(() => {});
    } catch (error) {
      if (controller.signal.aborted) break;
      console.warn("状态事件流已断开，继续轮询", error);
    } finally {
      const wasConnected = statusEventConnected;
      statusEventConnected = false;
      if (!controller.signal.aborted && (wasConnected || !statusPollTimer)) scheduleStatusPoll(STATUS_POLL_VISIBLE_MS);
    }
    if (!controller.signal.aborted) await new Promise((resolve) => window.setTimeout(resolve, 2500));
  }
  if (statusEventController === controller) statusEventController = null;
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
    $("#allowLan")?.addEventListener("change", () => {
      if ($("#allowLan").checked && $("#lanAuthMode").value === "none") {
        $("#lanAuthMode").value = "token";
        $("#lanAuthMode").dataset.previousAuthMode = "token";
        $("#lanToken").value = "";
      }
      renderNetworkSettings();
      markNetworkFormDirty();
    });
    $("#lanAuthMode")?.addEventListener("change", async (event) => {
      const previous = event.target.dataset.previousAuthMode || "token";
      if (event.target.value === "none" && $("#allowLan").checked) {
        const allowed = await confirmAction("关闭认证后，任何可访问代理端口的设备都可以使用代理。请确认当前网络可信。", { title: "启用无认证共享", confirmLabel: "关闭认证" });
        if (!allowed) event.target.value = previous === "none" ? "token" : previous;
      }
      event.target.dataset.previousAuthMode = event.target.value;
      syncLanAuthFields();
      renderNetworkSettings();
      markNetworkFormDirty();
    });
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
    speedTestForm.addEventListener("input", () => { speedTestFormDirty = true; updateSettingsSaveBar(); });
    speedTestForm.addEventListener("change", () => { speedTestFormDirty = true; updateSettingsSaveBar(); });
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
    logSettingsForm.addEventListener("input", () => { logSettingsDirty = true; updateSettingsSaveBar(); });
    logSettingsForm.addEventListener("change", () => { logSettingsDirty = true; updateSettingsSaveBar(); });
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
    syncForm.addEventListener("input", () => { markSyncFormDirty(); renderSync(); });
    syncForm.addEventListener("change", () => { markSyncFormDirty(); renderSync(); });
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
  $("#commandSearch")?.addEventListener("input", renderCommandResults);
  document.querySelectorAll("[data-log-tab]").forEach((tab) => tab.addEventListener("click", () => setActiveView(tab.dataset.logTab)));
  document.querySelectorAll("[data-node-tab]").forEach((tab) => tab.addEventListener("click", () => {
    currentNodeDetailTab = tab.dataset.nodeTab;
    renderNodeDetailDrawer();
    if (currentNodeDetailTab === "history") loadNodeHistory(currentNodeDetailId).catch((error) => showToast(error.message, "error"));
  }));
  $("#subscriptionDrawer")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) closeDrawer(event.currentTarget); });
  $("#nodeDetailDrawer")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) closeDrawer(event.currentTarget); });
  $("#confirmDialog")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) finishConfirmation(false); });
  $("#batchStartResultDialog")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) closeBatchStartResults(); });
  $("#commandPalette")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) closeCommandPalette(); });
  $("#browserNodeFilter")?.addEventListener("change", (event) => { browserPageFilter.node = event.target.value; renderBrowserPage(); });
  $("#browserGroupFilter")?.addEventListener("change", (event) => { browserPageFilter.group = event.target.value; renderBrowserPage(); });
  $("#browserStatusFilter")?.addEventListener("change", (event) => { browserPageFilter.status = event.target.value; renderBrowserPage(); });
  $("#browserSearchInput")?.addEventListener("input", (event) => { browserPageFilter.search = event.target.value; renderBrowserPage(); });
  $("#languageSelect")?.addEventListener("change", (event) => {
    i18n.setLocale(event.target.value);
    renderActiveView({ force: true });
    i18n.apply(document);
  });
  $("#nodeDensity")?.addEventListener("change", (event) => setNodeDensity(event.target.value));
  $("#settingsNodeDensity")?.addEventListener("change", (event) => setNodeDensity(event.target.value));
  $("#nodeViewport")?.addEventListener("scroll", () => {
    if (appState.nodes.length <= 300 || nodeVirtualFrame) return;
    nodeVirtualFrame = requestAnimationFrame(() => { nodeVirtualFrame = 0; renderNodes(); });
  }, { passive: true });
  document.querySelectorAll("[data-node-status]").forEach((button) => button.addEventListener("click", () => {
    appState.filters.status = button.dataset.nodeStatus || "all";
    document.querySelectorAll("[data-node-status]").forEach((item) => item.classList.toggle("is-active", item === button));
    if ($("#nodeViewport")) $("#nodeViewport").scrollTop = 0;
    renderNodes();
  }));
  $("#healthAutoSwitch")?.addEventListener("change", async (event) => {
    const next = event.target.checked;
    event.target.disabled = true;
    try {
      const result = await api("/api/settings/health", { auto_switch: next });
      appState.healthSettings = result.settings || { auto_switch: next };
      showToast(next ? "已启用健康检查自动切换" : "已关闭健康检查自动切换");
    } catch (error) {
      event.target.checked = !next;
      showToast(error.message, "error");
    } finally { event.target.disabled = false; }
  });
  $("#importForm")?.addEventListener("input", () => {
    const output = $("#subscriptionFilterPreview");
    if (subscriptionPreviewSignature !== subscriptionFormPreviewSignature()) output.textContent = "规则已更改，请重新预览。";
  });
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
  $("#browserConfigModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeBrowserConfig();
  });
  $("#kernelReleaseModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeKernelReleases();
  });
  $("#browserConfigForm").addEventListener("submit", submitBrowserConfig);
  $("#browserConfigTabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-config-tab]");
    if (tab) setBrowserConfigTab(tab.dataset.configTab);
  });
  $("#browserConfigBody").addEventListener("change", async (event) => {
    const target = event.target;
    if (target.id === "browserExtensionFile") {
      const file = target.files?.[0];
      target.value = "";
      try { await uploadBrowserExtension(file); } catch (error) { showToast(error.message, "error"); }
    } else if (target.id === "browserCookieFile") {
      const file = target.files?.[0];
      target.value = "";
      if (!file) return;
      if (file.size > 900_000) return showToast("Cookie 文件过大，请拆分后分批导入", "error");
      $("#browserCookieImport").value = await file.text();
    }
  });
  $("#browserList").addEventListener("change", (event) => {
    const target = event.target;
    if (target.id === "browserSelectAll") {
      const records = browserConfig.filterBrowserRecords(appState.browser_instances || [], browserPageFilter, nodeById);
      for (const record of records) {
        if (target.checked) appState.selectedBrowserIds.add(record.id);
        else appState.selectedBrowserIds.delete(record.id);
      }
      renderBrowserPage();
    } else if (target.classList.contains("browser-checkbox")) {
      if (target.checked) appState.selectedBrowserIds.add(target.dataset.instance);
      else appState.selectedBrowserIds.delete(target.dataset.instance);
      renderBrowserPage();
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
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openCommandPalette();
      return;
    }
    if (event.key === "/" && !event.metaKey && !event.ctrlKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName) && !document.activeElement?.isContentEditable) {
      event.preventDefault();
      setActiveView("nodes");
      $("#searchInput").focus();
      return;
    }
    if (event.key === "Escape" && !$("#confirmDialog").hidden) {
      finishConfirmation(false);
      return;
    }
    if (event.key === "Escape" && !$("#batchStartResultDialog").hidden) {
      closeBatchStartResults();
      return;
    }
    if (event.key === "Escape" && !$("#commandPalette").hidden) {
      closeCommandPalette();
      return;
    }
    if (event.key === "Escape" && !$("#nodeDetailDrawer").hidden) {
      closeDrawer($("#nodeDetailDrawer"));
      return;
    }
    if (event.key === "Escape" && !$("#subscriptionDrawer").hidden) {
      closeDrawer($("#subscriptionDrawer"));
      return;
    }
    if (event.key === "Escape" && !$("#remoteBrowserCredentialsModal").hidden) {
      closeRemoteBrowserCredentialsModal();
    } else if (event.key === "Escape" && !$("#remoteBrowserLaunchModal").hidden) {
      closeRemoteBrowserLaunchModal();
    } else if (event.key === "Escape" && !$("#coreUploadModal").hidden) {
      closeCoreUploadModal();
    } else if (event.key === "Escape" && !$("#browserConfigModal").hidden) {
      closeBrowserConfig();
    } else if (event.key === "Escape" && !$("#kernelReleaseModal").hidden) {
      closeKernelReleases();
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
    statusEventController?.abort();
  });
}

bindEvents();
// 初始视图:优先使用上次记忆的选择(在 <html> 上的 data-initial-view)。
setNodeDensity(localStorage.getItem("pb-node-density") || "compact");
i18n.setLocale(i18n.getLocale());
setActiveView(document.documentElement.getAttribute("data-initial-view") || "overview");
runStatusPoll({ reportError: true });
startStatusEvents();
