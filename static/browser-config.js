(function (root, factory) {
  const browserConfig = factory();
  if (typeof module === "object" && module.exports) module.exports = browserConfig;
  root.ProxyBridgeBrowserConfig = browserConfig;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // 选项取值必须与后端 browser_fingerprint.py 保持一致；标签只用于界面显示。
  const KEEP = "__keep__";
  const PLATFORM_OPTIONS = [
    ["auto", "跟随系统"],
    ["windows", "Windows"],
    ["macos", "macOS"],
    ["linux", "Linux"],
  ];
  const BRAND_OPTIONS = [
    ["Chrome", "Chrome"],
    ["Edge", "Edge"],
    ["Opera", "Opera"],
    ["Vivaldi", "Vivaldi"],
    ["", "Chromium（内核默认）"],
  ];
  const LOCALE_MODE_OPTIONS = [
    ["exit_ip", "按出口 IP"],
    ["region", "按节点地区"],
    ["custom", "自定义"],
  ];
  // 语言另有「跟随系统」：macOS 版 Chromium 的 Intl 区域只认系统语言，改成其他语言会被检测站识破。
  const LANGUAGE_MODE_OPTIONS = [["system", "跟随系统"], ...LOCALE_MODE_OPTIONS];
  // 批量修改「关闭伪装项」的预设；canvas,font 为 macOS 上通过 Pixelscan 的推荐组合。
  const SPOOFING_PRESET_OPTIONS = [
    ["canvas,font", "关闭 Canvas 与字体伪装（检测友好）"],
    ["none", "全部伪装开启"],
  ];
  const DETECTION_FRIENDLY_DISABLED = ["canvas", "font"];
  const GEOLOCATION_OPTIONS = [
    ["block", "禁止"],
    ["ask", "询问"],
  ];
  const HARDWARE_OPTIONS = [0, 2, 4, 6, 8, 10, 12, 16, 20, 24, 32].map((value) => [String(value), value ? `${value} 核` : "随种子"]);
  const SPOOFING_OPTIONS = [
    ["canvas", "Canvas"],
    ["audio", "Audio"],
    ["font", "字体"],
    ["clientrects", "ClientRects"],
    ["gpu", "WebGL / GPU"],
  ];
  const LOCALE_SOURCE_LABELS = {
    exit_ip: "出口 IP",
    region: "节点地区",
    region_fallback: "节点地区（出口查询失败）",
    system: "系统语言",
    custom: "自定义",
  };
  const ACTIVE_STATUSES = new Set(["running", "starting", "stopping"]);

  function optionLabel(options, value) {
    const match = options.find(([key]) => String(key) === String(value));
    return match ? match[1] : String(value || "");
  }

  function isActive(instance) {
    return ACTIVE_STATUSES.has(String(instance?.status || ""));
  }

  function fingerprintOf(instance) {
    return instance && typeof instance.fingerprint === "object" && instance.fingerprint ? instance.fingerprint : {};
  }

  // 列表中显示的指纹摘要：品牌 · 平台 · 时区来源。
  function fingerprintSummary(instance) {
    const fp = fingerprintOf(instance);
    const parts = [
      optionLabel(BRAND_OPTIONS, fp.brand || ""),
      fp.platform && fp.platform !== "auto" ? optionLabel(PLATFORM_OPTIONS, fp.platform) : "",
      `时区${optionLabel(LOCALE_MODE_OPTIONS, fp.timezone_mode || "region")}`,
    ].filter(Boolean);
    if (fp.language_mode === "system") parts.push("语言跟随系统");
    if (fp.geolocation === "block") parts.push("禁止定位");
    return parts.join(" · ");
  }

  function localeSourceLabel(source) {
    return LOCALE_SOURCE_LABELS[source] || String(source || "");
  }

  function cdpEndpoint(instance) {
    const port = Number(instance?.cdp_port || 0);
    const path = String(instance?.cdp_ws_path || "");
    if (!isActive(instance) || !Number.isInteger(port) || port < 1024 || port > 65535) return "";
    if (!/^\/devtools\/browser\/[A-Za-z0-9-]{8,64}$/.test(path)) return "";
    return `ws://127.0.0.1:${port}${path}`;
  }

  function parseLines(text) {
    return String(text || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function browserGroups(records) {
    const groups = new Set();
    for (const record of Array.isArray(records) ? records : []) {
      const group = String(record?.group || "").trim();
      if (group) groups.add(group);
    }
    return [...groups].sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  }

  // 浏览器页、批量操作与「停止全部」共用的筛选逻辑，保证作用范围一致。
  function filterBrowserRecords(records, filter, nodeById) {
    const query = String(filter?.search || "").trim().toLowerCase();
    return (Array.isArray(records) ? records : []).filter((instance) => {
      if (filter?.node && filter.node !== "all" && instance.node_id !== filter.node) return false;
      if (filter?.group && filter.group !== "all") {
        const group = String(instance.group || "");
        if (filter.group === "__none__" ? group : group !== filter.group) return false;
      }
      const running = isActive(instance);
      if (filter?.status === "running" && !running) return false;
      if (filter?.status === "stopped" && running) return false;
      if (query) {
        const node = typeof nodeById === "function" ? nodeById(instance.node_id) : null;
        const haystack = [instance.label, instance.name, instance.group, instance.node_name, node?.name];
        if (!haystack.some((value) => String(value || "").toLowerCase().includes(query))) return false;
      }
      return true;
    });
  }

  // 把表单值转换为 /api/browser/update 或 /api/browser/create 的请求体。
  // batch 模式下值为 KEEP 的字段不提交，保持各记录原值。
  function buildConfigPayload(values, { batch = false } = {}) {
    const payload = {};
    const fingerprint = {};
    const keep = (value) => batch && value === KEEP;
    const text = (value) => String(value ?? "").trim();
    if (!batch && values.name !== undefined) payload.name = text(values.name);
    if (values.label !== undefined && !keep(values.label) && (!batch || values.labelEnabled)) payload.label = text(values.label);
    if (values.group !== undefined && !keep(values.group) && (!batch || values.groupEnabled)) payload.group = text(values.group);
    for (const key of ["platform", "brand", "timezone_mode", "language_mode", "geolocation"]) {
      if (values[key] !== undefined && !keep(values[key])) fingerprint[key] = text(values[key]);
    }
    if (values.hardware_concurrency !== undefined && !keep(values.hardware_concurrency)) {
      fingerprint.hardware_concurrency = Number(values.hardware_concurrency || 0);
    }
    if (batch && values.spoofing_preset !== undefined && !keep(values.spoofing_preset)) {
      fingerprint.disable_spoofing = values.spoofing_preset === "none" ? [] : String(values.spoofing_preset).split(",");
    }
    if (!batch) {
      for (const key of ["platform_version", "brand_version", "timezone", "language", "accept_language", "window_size"]) {
        if (values[key] !== undefined) fingerprint[key] = text(values[key]);
      }
      if (Array.isArray(values.disable_spoofing)) fingerprint.disable_spoofing = values.disable_spoofing.slice();
      if (values.start_urls !== undefined) payload.start_urls = parseLines(values.start_urls);
      if (Array.isArray(values.extensions)) payload.extensions = values.extensions.slice();
    }
    if (Object.keys(fingerprint).length) payload.fingerprint = fingerprint;
    if (values.kernel_version !== undefined && !keep(values.kernel_version)) payload.kernel_version = text(values.kernel_version);
    if (values.cdp_enabled !== undefined && !keep(values.cdp_enabled)) {
      payload.cdp_enabled = values.cdp_enabled === true || values.cdp_enabled === "true";
    }
    return payload;
  }

  function cookieFileName(instance, format) {
    const base = String(instance?.label || instance?.name || "browser").replace(/[^\w.一-龥-]+/g, "-").replace(/^-+|-+$/g, "") || "browser";
    return `${base.slice(0, 60)}-cookies.${format === "netscape" ? "txt" : "json"}`;
  }

  return Object.freeze({
    KEEP,
    PLATFORM_OPTIONS,
    BRAND_OPTIONS,
    LOCALE_MODE_OPTIONS,
    LANGUAGE_MODE_OPTIONS,
    SPOOFING_PRESET_OPTIONS,
    DETECTION_FRIENDLY_DISABLED,
    GEOLOCATION_OPTIONS,
    HARDWARE_OPTIONS,
    SPOOFING_OPTIONS,
    optionLabel,
    isActive,
    fingerprintOf,
    fingerprintSummary,
    localeSourceLabel,
    cdpEndpoint,
    parseLines,
    browserGroups,
    filterBrowserRecords,
    buildConfigPayload,
    cookieFileName,
  });
});
