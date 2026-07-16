const assert = require("node:assert/strict");
const test = require("node:test");

const auth = require("../static/auth.js");

const TOKEN = "01234567-89AB-CDEF-0123-456789ABCDEF";

function makeBrowser({ hash = "", storageFails = false, state = null } = {}) {
  const values = new Map();
  const location = { hash, pathname: "/", search: "?view=nodes" };
  const history = {
    state,
    replacements: [],
    replaceState(nextState, _title, url) {
      this.state = nextState;
      this.replacements.push({ state: nextState, url });
      location.hash = "";
    },
  };
  const sessionStorage = {
    getItem(key) {
      if (storageFails) throw new Error("storage disabled");
      return values.get(key) || null;
    },
    setItem(key, value) {
      if (storageFails) throw new Error("storage disabled");
      values.set(key, value);
    },
    removeItem(key) {
      if (storageFails) throw new Error("storage disabled");
      values.delete(key);
    },
  };
  return { URLSearchParams, history, location, sessionStorage, values };
}

test("stores a fragment token in the current tab and clears the URL", () => {
  const browser = makeBrowser({ hash: `#token=${TOKEN}` });

  assert.equal(auth.loadManagementToken(browser), TOKEN);
  assert.equal(browser.values.get("proxybridge.managementToken"), TOKEN);
  assert.equal(browser.location.hash, "");
  assert.equal(browser.history.replacements[0].url, "/?view=nodes");
});

test("uses history state when session storage is disabled", () => {
  const browser = makeBrowser({ hash: `#token=${TOKEN}`, storageFails: true });

  assert.equal(auth.loadManagementToken(browser), TOKEN);
  assert.equal(browser.location.hash, "");
  assert.equal(browser.history.state["proxybridge.managementToken"], TOKEN);
  assert.equal(auth.loadManagementToken(browser), TOKEN);
});

test("clears tab state after an instance-token mismatch", () => {
  const browser = makeBrowser({ hash: `#token=${TOKEN}` });
  assert.equal(auth.loadManagementToken(browser), TOKEN);

  assert.equal(auth.validateResponseToken(browser, TOKEN, "different-token"), false);
  assert.equal(browser.values.has("proxybridge.managementToken"), false);
  assert.equal(browser.history.state["proxybridge.managementToken"], undefined);
});

test("keeps a valid token after a matching response", () => {
  const browser = makeBrowser({ hash: `#token=${TOKEN}` });
  assert.equal(auth.loadManagementToken(browser), TOKEN);

  assert.equal(auth.validateResponseToken(browser, TOKEN, TOKEN), true);
  assert.equal(browser.values.get("proxybridge.managementToken"), TOKEN);
});
