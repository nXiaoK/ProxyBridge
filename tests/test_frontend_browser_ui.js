const assert = require("node:assert/strict");
const test = require("node:test");

const browserUi = require("../static/browser-ui.js");

function credentialFields() {
  return {
    remoteUrl: { value: "" },
    username: { value: "" },
    password: { value: "" },
  };
}

test("keeps launched credentials in the success fields until explicitly cleared", () => {
  const fields = credentialFields();
  const view = {
    setup: { hidden: false },
    success: { hidden: true },
    dialog: {
      description: "",
      setAttribute(name, value) {
        if (name === "aria-describedby") this.description = value;
      },
    },
  };
  const credentials = {
    remoteUrl: "https://192.168.1.20:23000/",
    username: "alice",
    password: "correct-horse-battery-staple",
  };

  browserUi.setCredentialFields(fields, credentials);
  browserUi.setCredentialView(view, true);

  assert.deepEqual(browserUi.credentialFields(fields), credentials);
  assert.equal(view.setup.hidden, true);
  assert.equal(view.success.hidden, false);
  assert.equal(view.dialog.description, "remoteBrowserLaunchSuccessDescription");
  assert.equal(
    browserUi.formatCredentials(browserUi.credentialFields(fields)),
    "远程浏览器链接：https://192.168.1.20:23000/\n访问用户名：alice\n访问密码：correct-horse-battery-staple",
  );

  browserUi.clearCredentialFields(fields);
  browserUi.setCredentialView(view, false);
  assert.deepEqual(browserUi.credentialFields(fields), { remoteUrl: "", username: "", password: "" });
  assert.equal(view.setup.hidden, false);
  assert.equal(view.success.hidden, true);
  assert.equal(view.dialog.description, "remoteBrowserLaunchDescription");
});

test("uses the browser Clipboard API when it is available", async () => {
  const writes = [];
  const environment = {
    navigator: { clipboard: { writeText: async (value) => writes.push(value) } },
    document: {
      body: {},
      createElement() {
        throw new Error("DOM fallback should not run");
      },
    },
  };

  assert.equal(await browserUi.copyText("remote link", environment), true);
  assert.deepEqual(writes, ["remote link"]);
});

test("falls back to a temporary DOM textarea on an insecure HTTP page", async () => {
  let appended = null;
  let removed = false;
  let selected = false;
  let focusRestored = false;
  const activeElement = { focus: () => { focusRestored = true; } };
  const environment = {
    navigator: { clipboard: { writeText: async () => { throw new Error("insecure context"); } } },
    document: {
      activeElement,
      body: { appendChild: (element) => { appended = element; } },
      createElement(tagName) {
        assert.equal(tagName, "textarea");
        return {
          value: "",
          readOnly: false,
          style: {},
          setAttribute() {},
          select: () => { selected = true; },
          remove: () => { removed = true; },
        };
      },
      execCommand(command) {
        assert.equal(command, "copy");
        return true;
      },
    },
  };

  assert.equal(await browserUi.copyText("https://192.168.1.20:23000/", environment), true);
  assert.equal(appended.value, "https://192.168.1.20:23000/");
  assert.equal(appended.readOnly, true);
  assert.equal(selected, true);
  assert.equal(removed, true);
  assert.equal(focusRestored, true);
});

test("rejects unsafe remote URLs and accepts a credential-free HTTPS root URL", () => {
  assert.equal(
    browserUi.safeRemoteBrowserUrl("https://192.168.1.20:23000/", URL),
    "https://192.168.1.20:23000/",
  );
  for (const value of [
    "http://192.168.1.20:23000/",
    "https://alice:secret@192.168.1.20:23000/",
    "https://192.168.1.20:23000/login",
    "https://192.168.1.20:23000/?token=secret",
    "javascript:alert(1)",
  ]) {
    assert.equal(browserUi.safeRemoteBrowserUrl(value, URL), "", value);
  }
});

test("escapes malformed persisted instance IDs before using them in a CSS selector", () => {
  assert.equal(browserUi.cssEscape("0123456789abcdef", null), "\\30 123456789abcdef");
  const escaped = browserUi.cssEscape('broken\"] input[data-instance="other', null);
  assert.equal(escaped.includes('\\"'), true);
  assert.equal(escaped.includes("\\]"), true);
  assert.equal(escaped.includes("\\["), true);
});

test("sorts browser records by most recent use with migration fallbacks", () => {
  const records = [
    { id: "created", created_at: 500 },
    { id: "started", created_at: 100, last_started_at: 900 },
    { id: "stopped", created_at: 200, last_stopped_at: 800 },
    { id: "explicit", created_at: 50, last_used_at: 1000, last_started_at: 60 },
  ];

  assert.deepEqual(
    browserUi.sortBrowserInstancesByRecentUse(records).map((item) => item.id),
    ["explicit", "started", "stopped", "created"],
  );
  assert.deepEqual(records.map((item) => item.id), ["created", "started", "stopped", "explicit"]);
  assert.equal(
    browserUi.browserInstanceLastUsedAt({
      created_at: 100,
      last_started_at: 300,
      last_stopped_at: 400,
      last_used_at: 200,
    }),
    400,
  );
});
