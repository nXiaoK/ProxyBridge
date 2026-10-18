const assert = require("node:assert/strict");
const test = require("node:test");

const ui = require("../static/ui-core.js");

test("keyed diff only replaces changed rows and preserves an active row", () => {
  const operations = ui.planKeyedDiff(
    [{ key: "node:a", html: "same" }, { key: "node:b", html: "old" }, { key: "node:c", html: "gone" }],
    [{ key: "node:b", html: "new" }, { key: "node:a", html: "same" }, { key: "node:d", html: "new" }],
    "node:b",
  );
  assert.deepEqual(operations.map(({ type, key }) => [type, key]), [
    ["defer", "node:b"], ["keep", "node:a"], ["insert", "node:d"], ["remove", "node:c"],
  ]);
});

test("keyed diff reports replacement, removal, and rejects duplicate keys", () => {
  const operations = ui.planKeyedDiff(
    [{ key: "group:one", html: "old" }, { key: "node:a", html: "gone" }],
    [{ key: "group:one", html: "new" }],
  );
  assert.deepEqual(operations.map(({ type }) => type), ["replace", "remove"]);
  assert.throws(() => ui.planKeyedDiff([], [{ key: "a" }, { key: "a" }]), /Duplicate keyed row/);
});

test("masks credential-like path and query values while retaining URL context", () => {
  assert.equal(
    ui.maskSensitiveUrl("http://127.0.0.1:18787/pool/0123456789abcdef/list.json?token=secret"),
    "http://127.0.0.1:18787/pool/0123•••••/list.json?token=•••••",
  );
  assert.equal(ui.maskSensitiveUrl("not-a-url-secret"), "not-•••••cret");
});

test("subscription paths, unknown query names, fragments and short tokens stay private", () => {
  const masked = ui.maskSensitiveUrl("https://user:password@example.test/opaque-subscription-id-abcdef?subscription=privatevalue#privatefragment");
  assert.equal(masked, "https://example.test/•••••/abcdef?subscription=•••••#•••••");
  for (const secret of ["password", "opaque-subscription-id", "privatevalue", "privatefragment"]) {
    assert.equal(masked.includes(secret), false);
  }
  assert.equal(ui.maskSensitiveUrl("https://example.test/pool/short/get?strategy=health"),
    "https://example.test/pool/•••••/get?strategy=health");
  assert.equal(ui.maskSensitiveUrl("https://example.test/short?opaquevalue"),
    "https://example.test/•••••?•••••");
});

test("open menus remain protected after focus moves to a different control", () => {
  const focusedRow = { getAttribute: () => "node:a" };
  const menuRow = { getAttribute: () => "node:b" };
  const focusedInput = { closest: () => focusedRow };
  const menu = { closest: () => menuRow };
  const container = { contains: () => true, querySelectorAll: () => [menu] };
  const protectedKeys = ui.protectedRowKeys(container, "data-ui-key", { activeElement: focusedInput });
  assert.deepEqual([...protectedKeys], ["node:a", "node:b"]);
  const operations = ui.planKeyedDiff(
    [{ key: "node:a", html: "a" }, { key: "node:b", html: "b" }],
    [{ key: "node:a", html: "changed" }], protectedKeys,
  );
  assert.deepEqual(operations.map(({ type }) => type), ["defer", "defer-remove"]);
});
