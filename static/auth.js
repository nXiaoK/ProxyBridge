(function (root, factory) {
  const auth = factory();
  if (typeof module === "object" && module.exports) module.exports = auth;
  root.ProxyBridgeAuth = auth;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const headerName = "X-ProxyBridge-Instance";
  const storageKey = "proxybridge.managementToken";
  const tokenPattern = /^[A-Za-z0-9._~-]{1,128}$/;

  function validToken(value) {
    return typeof value === "string" && tokenPattern.test(value);
  }

  function historyState(browser) {
    const state = browser.history.state;
    if (!state || typeof state !== "object" || Array.isArray(state)) return {};
    return { ...state };
  }

  function replaceCurrentEntry(browser, state) {
    browser.history.replaceState(
      state,
      "",
      `${browser.location.pathname}${browser.location.search}`,
    );
  }

  function loadManagementToken(browser) {
    const fragment = new browser.URLSearchParams(browser.location.hash.replace(/^#/, ""));
    const fragmentToken = fragment.get("token") || "";
    if (validToken(fragmentToken)) {
      let storedInSession = false;
      try {
        browser.sessionStorage.setItem(storageKey, fragmentToken);
        storedInSession = true;
      } catch (_) {
        storedInSession = false;
      }

      const state = historyState(browser);
      if (storedInSession) delete state[storageKey];
      else state[storageKey] = fragmentToken;
      try {
        replaceCurrentEntry(browser, state);
      } catch (_) {
        // Keeping the fragment is the last-resort reload path.
      }
      return fragmentToken;
    }

    let storedToken = "";
    try {
      storedToken = browser.sessionStorage.getItem(storageKey) || "";
    } catch (_) {
      storedToken = "";
    }
    if (validToken(storedToken)) return storedToken;

    const fallbackToken = historyState(browser)[storageKey];
    return validToken(fallbackToken) ? fallbackToken : "";
  }

  function clearManagementToken(browser) {
    try {
      browser.sessionStorage.removeItem(storageKey);
    } catch (_) {
      // Continue with the history-state fallback cleanup.
    }
    const state = historyState(browser);
    delete state[storageKey];
    try {
      replaceCurrentEntry(browser, state);
    } catch (_) {
      // The in-memory caller still clears its token.
    }
  }

  function validateResponseToken(browser, expected, received) {
    if (!expected || received === expected) return true;
    clearManagementToken(browser);
    return false;
  }

  return Object.freeze({
    headerName,
    loadManagementToken,
    clearManagementToken,
    validateResponseToken,
  });
});
