const appState = {
  cores: {},
  features: { browser: true, browser_profile_sync: true, docker: false },
  runtime: { mode: "headless", docker: false },
  platform: null,
  fingerprint_chromium: null,
  active_browsers: [],
  browser_instances: [],
  subscriptions: [],
  nodes: [],
  bridges: [],
  tests: {},
  requestLogs: [],
  systemLogs: [],
  pool: null,
  network: null,
  networkStatus: "loading",
  activeView: "nodes",
  filters: {
    protocol: "all",
    region: "all",
    subscription: "all",
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
};

const managementAuth = window.ProxyBridgeAuth;
let managementToken = managementAuth.loadManagementToken(window);

const $ = (selector) => document.querySelector(selector);
let lastCoreLogTrigger = null;
let lastBrowserRecordTrigger = null;
let lastCoreLogTriggerKey = null;
let lastBrowserRecordTriggerKey = null;
let pendingModalFocusKey = null;
let coreLogRequestGeneration = 0;
let coreLogAbortController = null;
let kernelDownloadPromise = null;
let statusRequestPromise = null;
let statusAbortController = null;
let statusPollTimer = null;
let syncOperationPromise = null;
let networkSettingsSavePromise = null;
const actionInFlight = new Set();
const renderSignatures = new Map();

const STATUS_REQUEST_TIMEOUT_MS = 12_000;
const STATUS_POLL_VISIBLE_MS = 3_500;
const STATUS_POLL_HIDDEN_MS = 30_000;

// 侧边栏可切换的视图列表,顺序与 HTML 中 .nav-item / .view 一致。
const VIEW_IDS = ["nodes", "subscriptions", "bridges", "logs", "system", "settings", "sync"];

function browserEnabled() {
  return appState.features?.browser !== false;
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

// 通用网络设置表单：与 WebDAV 表单一样，状态轮询不能覆盖尚未保存的开关。
let networkFormHydrated = false;
let networkFormDirty = false;

function markNetworkFormDirty() {
  networkFormDirty = true;
}

function applyNetworkSettingsToForm(network, { force = false } = {}) {
  if (networkFormDirty && !force) return;
  const allowLan = $("#allowLan");
  if (!allowLan) return;
  allowLan.checked = Boolean(network?.allow_lan);
  networkFormHydrated = true;
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

function networkSettingsRenderSnapshot() {
  const network = appState.network;
  return {
    status: appState.networkStatus,
    allow_lan: network?.allow_lan ?? null,
    listen_host: network?.listen_host || "",
    configurable: network?.configurable ?? true,
    docker_managed: network?.docker_managed === true,
    lan_addresses: networkLanAddresses(network),
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
  const runtimeManaged = appState.network?.configurable === false;
  const disabled = unavailable || runtimeManaged || Boolean(networkSettingsSavePromise);
  [$("#allowLan"), $("#networkSaveButton")].filter(Boolean).forEach((control) => {
    control.disabled = disabled;
  });
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
    shareWarning.textContent = dockerManaged
      ? "请保持管理端口只发布到宿主机 127.0.0.1。代理端口也默认映射到宿主机回环地址；若改成 0.0.0.0，将允许其他设备连接且当前没有用户名或密码。"
      : "请仅在可信网络开启。代理端口没有用户名或密码，同一局域网或可达本机的私有 IPv4 / VPN 设备可能直接使用你的代理。Proxy Bridge 控制台不会因此对外开放。";
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
  if (appState.network?.configurable === false) {
    throw new Error("Docker 模式下网络范围由端口映射管理");
  }
  const allowLan = Boolean($("#allowLan")?.checked);
  const result = await api("/api/settings/network", { allow_lan: allowLan });
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

async function loadSyncStatus() {
  const result = await api("/api/sync/status");
  appState.sync = result.settings || null;
  appState.syncOpenssl = Boolean(result.openssl);
  renderSync();
  return result;
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

function browserInstancesForNode(nodeId) {
  const rank = { running: 0, starting: 1, stopped: 2, failed: 3 };
  return (appState.browser_instances || [])
    .filter((instance) => instance.node_id === nodeId)
    .slice()
    .sort((a, b) => {
      const statusDiff = (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
      if (statusDiff) return statusDiff;
      if (!!a.legacy !== !!b.legacy) return a.legacy ? -1 : 1;
      return Number(a.created_at || 0) - Number(b.created_at || 0);
    });
}

function runningBrowserInstancesForNode(nodeId) {
  return browserInstancesForNode(nodeId).filter((instance) => ["running", "starting"].includes(instance.status));
}

function browserStatusLabel(status) {
  const labels = {
    running: "运行中",
    starting: "启动中",
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
          browserEnabled: browserEnabled(),
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
      renderWhenChanged("subscriptions", appState.subscriptions, renderSubscriptions, force);
      break;
    case "bridges":
      renderWhenChanged(
        "bridges",
        { bridges: appState.bridges, pool: appState.pool },
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
        { logs: appState.requestLogs, filters: appState.logFilters },
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
      appState.features = status.features || { browser: true, browser_profile_sync: true, docker: false };
      appState.runtime = status.runtime || { mode: status.platform?.shell_mode || "headless", docker: false };
      appState.platform = status.platform || null;
      appState.cores = status.cores || {};
      appState.fingerprint_chromium = status.fingerprint_chromium || null;
      appState.active_browsers = status.active_browsers || [];
      appState.browser_instances = status.browser_instances || [];
      appState.subscriptions = status.subscriptions || [];
      appState.nodes = status.nodes || [];
      appState.bridges = status.bridges || [];
      appState.tests = status.tests || {};
      appState.requestLogs = status.request_logs || [];
      appState.systemLogs = status.system_logs || [];
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
      logs: (appState.requestLogs || []).length,
      system: (appState.systemLogs || []).length,
    },
    renderNav
  );
  renderWhenChanged(
    "cores",
    {
      cores: appState.cores,
      fingerprint: appState.fingerprint_chromium,
      busy: Boolean(kernelDownloadPromise),
      browserEnabled: browserEnabled(),
    },
    renderCores
  );
  const brandSubtitle = $("#brandSubtitle");
  if (brandSubtitle) {
    brandSubtitle.textContent = dockerRuntime() ? "Docker 控制台 · 订阅节点" : "订阅节点 · 本地代理";
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
      const runningCount = instances.filter((i) => ["running", "starting"].includes(i.status)).length;
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
  const kernelBusyClass = kernelDownloadPromise ? " is-busy" : "";
  const kernelBusyAttributes = kernelDownloadPromise ? ' disabled aria-busy="true"' : "";
  // 指纹内核状态:已安装显示 ready,缺失显示可点击的「下载指纹内核」按钮。
  const fpPill = !browserEnabled()
    ? ""
    : fpKernel
      ? `<button class="pill core-pill ok${kernelBusyClass}" type="button" data-action="download-kernel" aria-label="指纹内核已就绪，点击可重新下载" title="指纹内核已就绪：${escapeHtml(fpKernel)}"${kernelBusyAttributes}>指纹内核 ready</button>`
      : `<button class="pill core-pill warn${kernelBusyClass}" type="button" data-action="download-kernel" aria-label="下载指纹内核 fingerprint-chromium" title="点击自动下载安装指纹内核 fingerprint-chromium"${kernelBusyAttributes}>下载指纹内核</button>`;
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
  return select.value;
}

function renderFilters() {
  const protocols = [...new Set(appState.nodes.map((node) => node.protocol))];
  const regions = [...new Set(appState.nodes.map((node) => node.region))];
  appState.filters.protocol = setSelectOptions($("#protocolFilter"), protocols, appState.filters.protocol, "全部协议");
  appState.filters.region = setSelectOptions($("#regionFilter"), regions, appState.filters.region, "全部地区");

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
  const subFilter = appState.filters.subscription || "all";
  const byNode = subscriptionIndexByNodeId();
  return appState.nodes.filter((node) => {
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

  for (const group of groups.values()) {
    group.nodes.sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""), "zh-CN"));
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
  const browserActions = browserEnabled()
    ? `
          <button class="row-button" data-action="launch-browser" data-node="${escapeHtml(node.id)}" aria-label="新开不同指纹浏览器 ${escapeHtml(node.name)}" title="新开不同指纹浏览器：独立 Profile + 绑定该节点代理">${icon("browser")}<span>浏览器</span></button>
          <button class="row-button" data-action="browser-records" data-node="${escapeHtml(node.id)}" aria-label="查看 ${escapeHtml(node.name)} 的浏览器记录" title="浏览器记录">${icon("list")}<span>${escapeHtml(recordLabel)}</span></button>
          ${
            browserActive
              ? `<button class="row-button danger" data-action="stop-browser" data-node="${escapeHtml(node.id)}" aria-label="停止 ${escapeHtml(node.name)} 的浏览器" title="结束该节点的指纹浏览器进程，保留本地代理">${icon("stop")}<span>停止浏览器${runningBrowserCount > 1 ? ` ${runningBrowserCount}` : ""}</span></button>`
              : ""
          }
        `
    : "";
  return `
    <tr class="node-row ${bridge ? "is-running" : ""}" data-node-id="${escapeHtml(node.id)}">
      <td>
        <div class="name-cell">
          <span class="node-name">${escapeHtml(node.name)}</span>
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
      <td colspan="8">
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
        </button>
      </td>
    </tr>
  `;
}

function renderNodes() {
  const rows = $("#nodeRows");
  const empty = $("#emptyNodes");
  const nodes = filteredNodes();
  const groups = groupNodesBySubscription(nodes);
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
  if (getBtn) getBtn.dataset.text = links.get_random || "";

  const rows = $("#poolLinkRows");
  if (!rows) return;
  const items = [
    ["JSON 列表", links.list_json],
    ["文本列表", links.proxies_txt],
    ["随机获取", links.get_random],
  ].filter(([, url]) => url);
  if (!items.length) {
    rows.innerHTML = `<div class="pool-link-empty">启动本地代理后，这里会显示可复制的代理池链接。</div>`;
    return;
  }
  rows.innerHTML = items
    .map(
      ([label, url]) => `
      <div class="pool-link-row">
        <span class="pool-link-label">${escapeHtml(label)}</span>
        <code class="pool-link-url" title="${escapeHtml(url)}">${escapeHtml(url)}</code>
      </div>`
    )
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
  if ($("#coreLogModal").hidden && $("#browserRecordModal").hidden) {
    const shell = $(".app-shell");
    if (shell) {
      shell.inert = false;
      shell.removeAttribute("aria-hidden");
    }
  }
}

function findActionTrigger(key) {
  if (!key) return null;
  return [...document.querySelectorAll("button[data-action]")].find((button) => actionLockKey(button) === key) || null;
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

function compactPath(value) {
  const parts = String(value || "").split("/").filter(Boolean);
  return parts.length > 3 ? `.../${parts.slice(-3).join("/")}` : value || "-";
}

function renderBrowserRecords(nodeId) {
  const node = nodeById(nodeId);
  const instances = browserInstancesForNode(nodeId);
  const runningCount = instances.filter((instance) => ["running", "starting"].includes(instance.status)).length;
  $("#browserRecordTitle").textContent = `${node?.name || "节点"} 浏览器记录`;
  $("#browserRecordMeta").textContent = `${instances.length} 个记录 · ${runningCount} 个运行中`;
  const body = $("#browserRecordBody");
  if (!instances.length) {
    body.innerHTML = `
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

  body.innerHTML = instances
    .map((instance) => {
      const running = ["running", "starting"].includes(instance.status);
      const profilePath = instance.profile_path || "";
      const action = running ? "stop-browser-instance" : "launch-browser-instance";
      const actionLabel = running ? "停止" : "启动";
      const actionIcon = running ? "stop" : "browser";
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
              <span>${instance.legacy ? "默认浏览器" : "独立指纹"} · ${escapeHtml(formatTime(instance.created_at))}</span>
              ${pillLabel ? `<span class="browser-record-label-pill" title="${escapeHtml(pillLabel)}">${escapeHtml(pillLabel)}</span>` : ""}
            </div>
            <span class="status ${escapeHtml(instance.status || "stopped")}">${escapeHtml(browserStatusLabel(instance.status))}</span>
          </header>
          <div class="browser-record-grid">
            <span>指纹</span><code>${escapeHtml(instance.fingerprint_seed || "-")}</code>
            <span>Profile</span><code title="${escapeHtml(profilePath)}">${escapeHtml(compactPath(profilePath))}</code>
            <span>上次启动</span><code>${escapeHtml(formatTime(instance.last_started_at))}</code>
            <span>PID</span><code>${escapeHtml(instance.pid || "-")}</code>
          </div>
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
            <button class="row-button ${running ? "danger" : ""}" type="button" data-action="${action}" data-node="${escapeHtml(nodeId)}" data-instance="${escapeHtml(instance.id)}" aria-label="${actionLabel} ${escapeHtml(instance.name || "浏览器")}">
              ${icon(actionIcon)}<span>${actionLabel}</span>
            </button>
          </div>
        </article>
      `;
    })
    .join("");

  if (activeId) {
    const input = body.querySelector(`.browser-label-input[data-instance="${activeId}"]`);
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
  const input = document.querySelector(`.browser-label-input[data-instance="${instanceId}"]`);
  const label = String(input?.value || "").trim();
  await api("/api/browser/update", { instance_id: instanceId, label });
  await loadStatus({ forceFresh: true });
  if (appState.browserRecordNodeId) {
    renderBrowserRecords(appState.browserRecordNodeId);
  }
  showToast(label ? `标签已保存：${label}` : "标签已清空");
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
      const result = await withSyncOperation(() => api("/api/sync/pull", {}));
      if (result.settings) appState.sync = result.settings;
      await loadStatus({ forceFresh: true });
      if (result.skipped && result.reason === "already_latest") {
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
    try {
      if (!navigator.clipboard?.writeText) throw new Error();
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch {
      throw new Error("当前环境无法复制到剪贴板");
    }
  }

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

async function launchBrowser(nodeId, instanceId = "") {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  const payload = instanceId ? { node_id: nodeId, instance_id: instanceId } : { node_id: nodeId };
  const result = await api("/api/browser/launch", payload);
  await loadStatus({ forceFresh: true });
  showToast(`已启动${result.browser_name ? ` ${result.browser_name}` : "指纹浏览器"}：${result.proxy}`);
  return result;
}

// 停止某节点的指纹浏览器进程,保留其本地代理。
async function stopBrowser(nodeId, instanceId = "") {
  if (!browserEnabled()) throw new Error("当前运行模式不提供浏览器功能");
  const payload = instanceId ? { instance_id: instanceId } : { node_id: nodeId };
  const result = await api("/api/browser/stop", payload);
  await loadStatus({ forceFresh: true });
  showToast("已停止浏览器，SOCKS5 端口保持运行");
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
    case "launch-browser":
    case "stop-browser":
      return `browser-node:${button.dataset.node || ""}`;
    case "launch-browser-instance":
    case "stop-browser-instance":
    case "save-browser-label":
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
    "close-browser-records",
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
      await launchBrowser(button.dataset.node);
    } else if (action === "launch-browser-instance") {
      await launchBrowser(button.dataset.node, button.dataset.instance);
    } else if (action === "stop-browser") {
      await stopBrowser(button.dataset.node);
    } else if (action === "stop-browser-instance") {
      await stopBrowser(button.dataset.node, button.dataset.instance);
    } else if (action === "browser-records") {
      await openBrowserRecordsWithSync(button.dataset.node, button);
    } else if (action === "save-browser-label") {
      await saveBrowserLabel(button.dataset.instance);
    } else if (action === "download-kernel") {
      await downloadKernel();
    } else if (action === "copy") {
      await copyText(button.dataset.text);
    } else if (action === "rotate-pool-token") {
      if (!window.confirm("轮换代理池密钥后，所有旧链接会立即失效。确定继续吗？")) return;
      await api("/api/pool/token/rotate", {});
      await loadStatus({ forceFresh: true });
      showToast("代理池密钥已轮换，旧链接立即失效");
    } else if (action === "stop") {
      const result = await api("/api/bridges/stop", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast(result.killed_browsers ? "已停止端口，关联浏览器已关闭" : "已停止端口");
    } else if (action === "stop-all") {
      const result = await api("/api/bridges/stop-all", {});
      await loadStatus({ forceFresh: true });
      showToast(result.killed_browsers ? "已停止全部端口，关联浏览器已关闭" : "已停止全部端口");
    } else if (action === "restart") {
      const stopped = await api("/api/bridges/stop", { id: button.dataset.id });
      await api("/api/bridges/start", { node_id: button.dataset.node, core: "auto" });
      await loadStatus({ forceFresh: true });
      showToast(stopped.killed_browsers ? "已重启服务，关联浏览器已关闭" : "已重启服务");
    } else if (action === "refresh-sub") {
      await api("/api/subscriptions/refresh", { id: button.dataset.id });
      await loadStatus({ forceFresh: true });
      showToast("订阅已刷新");
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
      showToast("连接日志已清空");
    } else if (action === "clear-system-logs") {
      await api("/api/system/logs/clear", {});
      await loadStatus({ forceFresh: true });
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
      lastCoreLogTriggerKey = lockKey;
      await showCoreLog(button.dataset.core);
    } else if (action === "close-core-log") {
      closeCoreLogModal();
    } else if (action === "close-browser-records") {
      closeBrowserRecordModal();
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
    await loadStatus({ forceFresh: true });
    showToast(`导入 ${result.subscription.node_count} 个节点`);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

function trapModalFocus(event) {
  if (event.key !== "Tab") return false;
  const modal = !$("#browserRecordModal").hidden
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
  const networkForm = $("#networkSettingsForm");
  if (networkForm) {
    networkForm.addEventListener("input", markNetworkFormDirty);
    networkForm.addEventListener("change", markNetworkFormDirty);
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
    button.disabled = true;
    try {
      for (const [index, node] of nodes.entries()) {
        button.querySelector("span").textContent = `测试中 ${index + 1}/${nodes.length}`;
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
  $("#browserRecordModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeBrowserRecordModal();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (trapModalFocus(event)) return;
    if (event.key === "Escape" && !$("#coreLogModal").hidden) {
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
    statusAbortController?.abort();
  });
}

bindEvents();
// 初始视图:优先使用上次记忆的选择(在 <html> 上的 data-initial-view)。
setActiveView(document.documentElement.getAttribute("data-initial-view") || "nodes");
runStatusPoll({ reportError: true });
