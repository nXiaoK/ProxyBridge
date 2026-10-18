const assert = require("node:assert/strict");
const test = require("node:test");

const i18n = require("../static/i18n.js");

class FakeTextNode {
  constructor(value) {
    this.nodeType = 3;
    this.nodeValue = value;
  }
}

class FakeElement {
  constructor({ tagName = "div", text = "", attrs = {}, children = [], value = undefined } = {}) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.childNodes = text ? [new FakeTextNode(text), ...children] : children;
    this.attributes = new Map(Object.entries(attrs));
    this.value = value;
    this.dataset = {};
  }

  get textContent() {
    return this.childNodes.map((child) => child.nodeValue ?? child.textContent ?? "").join("");
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  matches(selector) {
    return selector.split(",").some((part) => part.trim().toLowerCase() === this.tagName.toLowerCase());
  }
}

function fakeDocument(matches = new Map()) {
  return {
    documentElement: { dataset: {} },
    querySelectorAll(selector) {
      if (selector === "[data-i18n]") return matches.get("[data-i18n]") || [];
      if (selector.startsWith("[data-i18n-")) return matches.get(selector) || [];
      return matches.get(selector) || [];
    },
    createTreeWalker() {
      throw new Error("apply must not walk arbitrary document text");
    },
  };
}

test("translates exact UI phrases without translating substrings or dynamic values", () => {
  assert.equal(i18n.translate("概览", "en"), "Overview");
  assert.equal(i18n.translate("WebDAV Sync", "zh-CN"), "WebDAV 同步");
  assert.equal(i18n.translate("untranslated server data", "en"), "untranslated server data");
  assert.equal(i18n.translate("自定义节点 Overview", "en"), "自定义节点 Overview");
  assert.equal(i18n.translate("200 nodes", "zh-CN"), "200 nodes");
});

test("translates static form labels, buttons, and placeholders and restores their source copy", () => {
  const saveButton = new FakeElement({ text: "上传到远端" });
  const usernameLabel = new FakeElement({ text: "用户名" });
  const password = new FakeElement({
    attrs: { placeholder: "已保存则留空不改" },
    value: "existing-secret",
  });
  const url = new FakeElement({
    attrs: { placeholder: "https://dav.example.com/dav" },
    value: "https://dav.example.com/private?token=keep-me",
  });
  const customNodeName = new FakeElement({ text: "概览" });
  const userLog = new FakeElement({ text: "连接日志" });
  const userRawContent = new FakeElement({
    tagName: "textarea",
    text: "概览",
    attrs: { "data-i18n": "static-but-input-value" },
    value: "概览",
  });
  const markedText = new FakeElement({
    attrs: { "data-i18n": "static.nav.overview" },
    children: [new FakeElement({ text: "概览" })],
  });
  const matches = new Map([
    ["#syncPushBtn", [saveButton]],
    ["#syncForm label > span", [usernameLabel]],
    ["#syncPassword", [password]],
    ["[data-i18n]", [markedText, userRawContent]],
  ]);
  const documentObject = fakeDocument(matches);
  const values = new Map();
  const storage = {
    setItem: (key, value) => values.set(key, value),
    getItem: (key) => values.get(key),
  };

  i18n.setLocale("en", { documentObject, storage });
  assert.equal(saveButton.textContent, "Upload to Remote");
  assert.equal(usernameLabel.textContent, "Username");
  assert.equal(password.getAttribute("placeholder"), "Leave blank to keep the saved value");
  assert.equal(password.value, "existing-secret");
  assert.equal(url.getAttribute("placeholder"), "https://dav.example.com/dav");
  assert.equal(url.value, "https://dav.example.com/private?token=keep-me");
  assert.equal(customNodeName.textContent, "概览");
  assert.equal(userLog.textContent, "连接日志");
  assert.equal(userRawContent.textContent, "概览");
  assert.equal(userRawContent.value, "概览");
  assert.equal(markedText.textContent, "Overview");

  i18n.setLocale("zh-CN", { documentObject, storage });
  assert.equal(saveButton.textContent, "上传到远端");
  assert.equal(usernameLabel.textContent, "用户名");
  assert.equal(password.getAttribute("placeholder"), "已保存则留空不改");
  assert.equal(markedText.textContent, "概览");
  assert.equal(documentObject.documentElement.lang, "zh-CN");
  assert.equal(values.get("pb-locale"), "zh-CN");
});

test("translates only fixed labels inside dynamic rows and keeps user names and labels intact", () => {
  const nodeName = new FakeElement({ text: "概览" });
  const nodeStart = new FakeElement({ text: "启动" });
  const browserLabel = new FakeElement({ text: "启动" });
  const browserStart = new FakeElement({ text: "启动" });
  const browserState = new FakeElement({ text: "启动中" });
  const subscriptionName = new FakeElement({ text: "Start" });
  const subscriptionRefresh = new FakeElement({ text: "刷新" });
  const bridgeStop = new FakeElement({ text: "停止" });
  const menuSummary = new FakeElement({ text: "复制 ▾" });
  const browserHistoryCount = new FakeElement({ text: "最近刷新记录 · 3" });
  const browserRecordsCount = new FakeElement({ text: "浏览器记录 2" });
  const drawerTab = new FakeElement({ text: "配置" });
  const browserHeader = new FakeElement({ tagName: "th", text: "绑定节点" });
  const userLog = new FakeElement({ text: "连接日志" });
  const matches = new Map([
    [".node-row button[data-action='start']", [nodeStart]],
    ["#browserListRows button[data-action='launch-browser-instance']", [browserStart]],
    ["#browserListRows .status-pill", [browserState]],
    [".subscription-item button[data-action='refresh-sub'] span", [subscriptionRefresh]],
    [".bridge-card .bridge-actions button[data-action='stop'] span", [bridgeStop]],
    [".node-row .copy-menu > summary", [menuSummary]],
    [".node-row .row-actions button[data-action='browser-records']", [browserRecordsCount]],
    [".subscription-item details.subscription-history > summary", [browserHistoryCount]],
    ["#nodeDetailDrawer .drawer-tabs [data-node-tab]", [drawerTab]],
    [".responsive-table thead th", [browserHeader]],
  ]);
  const documentObject = fakeDocument(matches);

  i18n.setLocale("en", { documentObject, storage: null });
  assert.equal(nodeStart.textContent, "Start");
  assert.equal(browserStart.textContent, "Start");
  assert.equal(browserState.textContent, "Starting");
  assert.equal(subscriptionRefresh.textContent, "Refresh");
  assert.equal(bridgeStop.textContent, "Stop");
  assert.equal(menuSummary.textContent, "Copy ▾");
  assert.equal(browserRecordsCount.textContent, "Browser Records 2");
  assert.equal(browserHistoryCount.textContent, "Recent Refresh History · 3");
  assert.equal(drawerTab.textContent, "Configuration");
  assert.equal(browserHeader.textContent, "Bound Node");
  assert.equal(nodeName.textContent, "概览");
  assert.equal(browserLabel.textContent, "启动");
  assert.equal(subscriptionName.textContent, "Start");
  assert.equal(userLog.textContent, "连接日志");

  i18n.setLocale("zh-CN", { documentObject, storage: null });
  assert.equal(nodeStart.textContent, "启动");
  assert.equal(browserStart.textContent, "启动");
  assert.equal(browserState.textContent, "启动中");
  assert.equal(browserRecordsCount.textContent, "浏览器记录 2");
  assert.equal(browserHistoryCount.textContent, "最近刷新记录 · 3");
  assert.equal(nodeName.textContent, "概览");
  assert.equal(browserLabel.textContent, "启动");
  assert.equal(subscriptionName.textContent, "Start");
});

test("translates pool controls while preserving the access URL input value", () => {
  const guide = new FakeElement({ text: "启动节点后会自动加入代理池。" });
  const showLinks = new FakeElement({
    text: "显示访问链接",
    attrs: { title: "显示或隐藏访问链接" },
  });
  const rotateToken = new FakeElement({ text: "轮换密钥" });
  const accessLabel = new FakeElement({ text: "只读访问链接" });
  const jsonFormat = new FakeElement({ text: "JSON" });
  const textFormat = new FakeElement({ text: "文本" });
  const healthFormat = new FakeElement({ text: "健康优先" });
  const accessUrl = new FakeElement({
    tagName: "input",
    attrs: { "aria-label": "只读代理池访问链接", title: "token=secret" },
    value: "https://proxy.example/pool/secret-token",
  });
  const revealLink = new FakeElement({
    attrs: { "aria-label": "隐藏访问链接", title: "隐藏敏感链接" },
  });
  const matches = new Map([
    ["#poolEmptyGuide", [guide]],
    ["#poolShowLinks", [showLinks]],
    ["#poolRotateToken", [rotateToken]],
    ["#poolCard .pool-access-field label > span", [accessLabel]],
    ["#poolLinkFormats button[data-pool-format]", [jsonFormat, textFormat, healthFormat]],
    ["#poolAccessUrl", [accessUrl]],
    ["#poolRevealLink", [revealLink]],
  ]);
  const documentObject = fakeDocument(matches);

  i18n.setLocale("en", { documentObject, storage: null });
  assert.equal(guide.textContent, "Start a node to add it to the proxy pool automatically.");
  assert.equal(showLinks.textContent, "Show Access Links");
  assert.equal(showLinks.getAttribute("title"), "Show or hide access links");
  assert.equal(rotateToken.textContent, "Rotate Token");
  assert.equal(accessLabel.textContent, "Read-only Access Link");
  assert.equal(jsonFormat.textContent, "JSON");
  assert.equal(textFormat.textContent, "Text");
  assert.equal(healthFormat.textContent, "Health First");
  assert.equal(accessUrl.getAttribute("aria-label"), "Read-only Proxy Pool Access Link");
  assert.equal(accessUrl.getAttribute("title"), "token=secret");
  assert.equal(accessUrl.value, "https://proxy.example/pool/secret-token");
  assert.equal(revealLink.getAttribute("aria-label"), "Hide Access Links");
  assert.equal(revealLink.getAttribute("title"), "Hide Sensitive Link");

  guide.childNodes[0].nodeValue = "运行中的代理会自动加入，可按需临时排除成员。";
  showLinks.childNodes[0].nodeValue = "隐藏访问链接";
  i18n.apply(documentObject, "en");
  assert.equal(guide.textContent, "Running proxies join automatically; exclude members temporarily when needed.");
  assert.equal(showLinks.textContent, "Hide Access Links");

  i18n.setLocale("zh-CN", { documentObject, storage: null });
  assert.equal(guide.textContent, "运行中的代理会自动加入，可按需临时排除成员。");
  assert.equal(showLinks.textContent, "隐藏访问链接");
  assert.equal(rotateToken.textContent, "轮换密钥");
  assert.equal(accessLabel.textContent, "只读访问链接");
  assert.equal(textFormat.textContent, "文本");
  assert.equal(healthFormat.textContent, "健康优先");
  assert.equal(accessUrl.value, "https://proxy.example/pool/secret-token");
});

test("locale preference is normalized and saved", () => {
  const values = new Map();
  const storage = {
    setItem: (key, value) => values.set(key, value),
    getItem: (key) => values.get(key),
  };
  const documentObject = fakeDocument();

  assert.equal(i18n.setLocale("en", { documentObject, storage }), "en");
  assert.equal(documentObject.documentElement.lang, "en");
  assert.equal(values.get("pb-locale"), "en");
  assert.equal(i18n.getLocale(storage), "en");
  assert.equal(i18n.setLocale("fr", { documentObject, storage }), "zh-CN");
});
