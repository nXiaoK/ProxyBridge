const assert = require("node:assert/strict");
const test = require("node:test");
const { createOverviewRenderer } = require("../static/pages/overview.js");
const { createKernelRenderer } = require("../static/pages/kernels.js");
const { createBrowserRenderer } = require("../static/pages/browsers.js");
const { createNodeRenderer } = require("../static/pages/nodes.js");
const browserConfig = require("../static/browser-config.js");

const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
const element = () => ({ innerHTML: "", textContent: "", hidden: false, classList: { toggle() {}, remove() {} } });

test("overview error groups search the actual category when no node is associated", () => {
  const content = element();
  const status = element();
  const render = createOverviewRenderer({
    $: (selector) => selector === "#overviewContent" ? content : status,
    appState: { nodes: [], subscriptions: [{}], tests: {}, systemLogs: [{ level: "error", category: "核心", message: "failed", ts: 1 }],
      logCenter: { system_error_24h: 1, error_groups: [{ category: "核心", count: 1 }] } },
    runningBridges: () => [], escapeHtml, formatLogTime: () => "10:00", icon: () => "",
  });
  render();
  assert.match(content.innerHTML, /data-search="核心"/);
  assert.match(content.innerHTML, /10:00/);
  assert.doesNotMatch(content.innerHTML, /data-search="系统"/);
});

test("kernel fallback puts the binary path in the path column and leaves version unknown", () => {
  const list = element();
  const render = createKernelRenderer({
    $: () => list, appState: { cores: { "sing-box": "/tmp/sing-box" }, coreCatalog: [] },
    browserEnabled: () => false, escapeHtml,
  });
  render();
  assert.match(list.innerHTML, /<code title="\/tmp\/sing-box">\/tmp\/sing-box<\/code>/);
  assert.doesNotMatch(list.innerHTML, /<td>\/tmp\/sing-box<\/td>/);
});

test("browser renderer handles real records without reporting unknown node regions as mismatches", () => {
  const body = element();
  const list = { ...element(), querySelector: () => body };
  const node = { id: "node", name: "Node", region_code: "ZZ" };
  const render = createBrowserRenderer({
    $: (selector) => selector === "#browserNodeFilter" ? null : selector === "#browserList" ? list : selector === "#browserListRows" ? body : element(),
    appState: { nodes: [node], browser_instances: [{ id: "browser", node_id: "node", label: "Start", status: "stopped", exit_check: { ip: "1.1.1.1", region_code: "US" } }] },
    browserPageFilter: { node: "all", status: "all", search: "start" },
    browserUi: { sortBrowserInstancesByRecentUse: (records) => records, browserInstanceLastUsedAt: () => 1 },
    browserConfig,
    browserEnabled: () => true, remoteBrowserEnabled: () => false, nodeById: () => node,
    escapeHtml, safeRemoteBrowserUrl: () => "", formatRelativeTime: () => "now", browserStatusLabel: () => "已停止",
    uiCore: { patchKeyedHtml: (target, html) => { target.innerHTML = html; } },
  });
  render();
  assert.match(body.innerHTML, /data-ui-key="browser:browser"/);
  assert.match(body.innerHTML, /1\.1\.1\.1/);
  assert.doesNotMatch(body.innerHTML, /地区不一致/);
});

test("node page module renders stable keys and escapes node names", (t) => {
  const previousDocument = global.document;
  const previousComputedStyle = global.getComputedStyle;
  global.document = { documentElement: { dataset: { nodeDensity: "compact" } } };
  global.getComputedStyle = () => ({ getPropertyValue: () => "36px" });
  t.after(() => { global.document = previousDocument; global.getComputedStyle = previousComputedStyle; });
  const rows = element();
  const empty = element();
  const node = { id: "node", name: "<img onerror=bad>", server: "example.test", port: 443, protocol: "vless", region_code: "US" };
  const render = createNodeRenderer({
    $: (selector) => selector === "#nodeRows" ? rows : selector === "#emptyNodes" ? empty : element(),
    appState: { nodes: [node], selectedNodeIds: new Set(), filters: { search: "" } },
    bridgeForNode: () => null, mostRecentBridgeForNode: () => null, browserEnabled: () => false,
    browserInstancesForNode: () => [], runningBrowserInstancesForNode: () => [], escapeHtml,
    icon: () => "", latencyCell: () => "未测试", isNodeGroupCollapsed: () => false,
    groupAverageLatency: () => "—", initials: () => "G", filteredNodes: () => [node],
    groupNodesBySubscription: () => [{ id: "group", name: "Group", kind: "source", nodes: [node] }],
    renderSpeedTestTaskControls() {}, updateBatchActionsBar() {}, bindNodeCheckboxes() {},
    uiCore: { patchKeyedHtml: (target, html) => { target.innerHTML = html; } },
  });
  render();
  assert.match(rows.innerHTML, /data-ui-key="node:node"/);
  assert.match(rows.innerHTML, /&lt;img onerror=bad>/);
  assert.doesNotMatch(rows.innerHTML, /<img onerror/);
});
