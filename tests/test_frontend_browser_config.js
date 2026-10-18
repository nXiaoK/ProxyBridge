const assert = require("node:assert/strict");
const test = require("node:test");
const browserConfig = require("../static/browser-config.js");

test("single edit payload carries full fingerprint and keeps custom locale drafts", () => {
  const payload = browserConfig.buildConfigPayload({
    name: " 店铺 A ",
    label: "",
    group: "电商",
    platform: "windows",
    brand: "Chrome",
    hardware_concurrency: "8",
    platform_version: "",
    brand_version: "",
    window_size: "1920x1080",
    timezone_mode: "exit_ip",
    timezone: "Europe/Paris",
    language_mode: "custom",
    language: "fr-FR",
    accept_language: "",
    geolocation: "block",
    disable_spoofing: ["gpu"],
    extensions: ["abcdef0123456789"],
    kernel_version: "",
    cdp_enabled: true,
    start_urls: "https://a.test/\n\n https://b.test/ ",
  });
  assert.equal(payload.name, "店铺 A");
  assert.equal(payload.group, "电商");
  assert.deepEqual(payload.start_urls, ["https://a.test/", "https://b.test/"]);
  assert.equal(payload.fingerprint.hardware_concurrency, 8);
  // 非自定义模式下保留填写过的自定义时区，切回「自定义」时无需重填。
  assert.equal(payload.fingerprint.timezone, "Europe/Paris");
  assert.equal(payload.fingerprint.language, "fr-FR");
  assert.deepEqual(payload.fingerprint.disable_spoofing, ["gpu"]);
  assert.equal(payload.cdp_enabled, true);
  assert.equal(payload.kernel_version, "");
});

test("batch payload omits keep values and unchecked text fields", () => {
  const { KEEP } = browserConfig;
  const payload = browserConfig.buildConfigPayload({
    label: "x", labelEnabled: false,
    group: "", groupEnabled: true,
    platform: KEEP, brand: "Edge", hardware_concurrency: KEEP,
    timezone_mode: KEEP, language_mode: "region", geolocation: KEEP,
    kernel_version: KEEP, cdp_enabled: "false",
  }, { batch: true });
  assert.deepEqual(payload, { group: "", fingerprint: { brand: "Edge", language_mode: "region" }, cdp_enabled: false });
  assert.deepEqual(browserConfig.buildConfigPayload({ platform: KEEP, kernel_version: KEEP, cdp_enabled: KEEP, spoofing_preset: KEEP }, { batch: true }), {});
  assert.deepEqual(browserConfig.buildConfigPayload({ spoofing_preset: "canvas,font", language_mode: "system" }, { batch: true }).fingerprint, { language_mode: "system", disable_spoofing: ["canvas", "font"] });
  assert.deepEqual(browserConfig.buildConfigPayload({ spoofing_preset: "none" }, { batch: true }).fingerprint, { disable_spoofing: [] });
});

test("record filter supports group, status, node and search", () => {
  const records = [
    { id: "a", node_id: "n1", group: "电商", status: "running", name: "浏览器 1" },
    { id: "b", node_id: "n2", group: "", status: "stopped", name: "浏览器 2", label: "广告" },
    { id: "c", node_id: "n1", group: "社媒", status: "stopped", name: "浏览器 3" },
  ];
  const ids = (filter) => browserConfig.filterBrowserRecords(records, filter, () => null).map((item) => item.id);
  assert.deepEqual(ids({ group: "电商", node: "all", status: "all" }), ["a"]);
  assert.deepEqual(ids({ group: "__none__", node: "all", status: "all" }), ["b"]);
  assert.deepEqual(ids({ group: "all", node: "n1", status: "stopped" }), ["c"]);
  assert.deepEqual(ids({ group: "all", node: "all", status: "all", search: "社媒" }), ["c"]);
  assert.deepEqual(ids({ group: "all", node: "all", status: "running", search: "" }), ["a"]);
  assert.deepEqual(browserConfig.browserGroups(records), ["电商", "社媒"].sort((x, y) => x.localeCompare(y, "zh-Hans-CN")));
});

test("cdp endpoint only exists for running loopback records", () => {
  const record = { status: "running", cdp_port: 45678, cdp_ws_path: "/devtools/browser/abcdef12-3456" };
  assert.equal(browserConfig.cdpEndpoint(record), "ws://127.0.0.1:45678/devtools/browser/abcdef12-3456");
  assert.equal(browserConfig.cdpEndpoint({ ...record, status: "stopped" }), "");
  assert.equal(browserConfig.cdpEndpoint({ ...record, cdp_ws_path: "/json" }), "");
  assert.equal(browserConfig.cdpEndpoint({ ...record, cdp_port: 80 }), "");
});

test("fingerprint summary and labels", () => {
  assert.equal(
    browserConfig.fingerprintSummary({ fingerprint: { brand: "Chrome", platform: "windows", timezone_mode: "exit_ip", geolocation: "block" } }),
    "Chrome · Windows · 时区按出口 IP · 禁止定位",
  );
  assert.equal(browserConfig.fingerprintSummary({ fingerprint: { brand: "", platform: "auto", timezone_mode: "region" } }), "Chromium（内核默认） · 时区按节点地区");
  assert.equal(browserConfig.localeSourceLabel("region_fallback"), "节点地区（出口查询失败）");
  assert.equal(browserConfig.cookieFileName({ label: "店铺 A/B" }, "netscape"), "店铺-A-B-cookies.txt");
});
