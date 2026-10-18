(function (root) {
  "use strict";

  function planKeyedDiff(current, next, activeKey = null) {
    const protectedKeys = activeKey instanceof Set ? activeKey : new Set(activeKey == null ? [] : [String(activeKey)]);
    const currentByKey = new Map(current.map((item, index) => [String(item.key), { ...item, index }]));
    const nextKeys = new Set();
    const operations = [];
    for (const item of next) {
      const key = String(item.key);
      if (nextKeys.has(key)) throw new Error(`Duplicate keyed row: ${key}`);
      nextKeys.add(key);
      const previous = currentByKey.get(key);
      if (!previous) operations.push({ type: "insert", key, item });
      else if (protectedKeys.has(key)) operations.push({ type: "defer", key, item });
      else if (previous.html === item.html) operations.push({ type: "keep", key, item });
      else operations.push({ type: "replace", key, item });
    }
    for (const item of current) {
      const key = String(item.key);
      if (!nextKeys.has(key)) {
        operations.push({ type: protectedKeys.has(key) ? "defer-remove" : "remove", key, item });
      }
    }
    return operations;
  }

  function protectedRowKeys(container, keyAttribute, documentObject) {
    const keys = new Set();
    const active = documentObject?.activeElement;
    const remember = (element) => {
      const row = element?.closest?.(`[${keyAttribute}]`);
      if (row && container.contains(row)) keys.add(row.getAttribute(keyAttribute));
    };
    if (active && container.contains(active)) remember(active);
    for (const menu of container.querySelectorAll("details[open]")) remember(menu);
    return keys;
  }

  function patchKeyedHtml(container, html, { keyAttribute = "data-ui-key", documentObject = root.document } = {}) {
    if (!container || !documentObject) return [];
    const template = documentObject.createElement("template");
    template.innerHTML = String(html || "");
    const incoming = Array.from(template.content.children);
    const current = Array.from(container.children);
    const currentItems = current.map((element) => ({
      key: element.getAttribute(keyAttribute),
      html: element.dataset.uiHtml || element.outerHTML,
      element,
    }));
    const nextItems = incoming.map((element) => ({
      key: element.getAttribute(keyAttribute),
      html: element.outerHTML,
      element,
    }));
    const protectedKeys = protectedRowKeys(container, keyAttribute, documentObject);
    const operations = planKeyedDiff(currentItems, nextItems, protectedKeys);
    const oldByKey = new Map(currentItems.map((item) => [item.key, item.element]));
    const nextElements = [];

    for (const operation of operations) {
      if (["insert", "keep", "replace", "defer"].includes(operation.type)) {
        let element;
        if (operation.type === "insert") {
          element = operation.item.element;
        } else {
          element = oldByKey.get(operation.key);
          if (operation.type === "replace") {
            container.replaceChild(operation.item.element, element);
            element = operation.item.element;
          }
        }
        element.dataset.uiHtml = operation.type === "defer"
          ? (element.dataset.uiHtml || element.outerHTML)
          : operation.item.html;
        nextElements.push(element);
      } else if (operation.type === "defer-remove") {
        const element = oldByKey.get(operation.key);
        if (element) nextElements.push(element);
      }
    }

    const protectedElements = new Set(current.filter((element) => protectedKeys.has(element.getAttribute(keyAttribute))));
    // 操作中的行保持原有位置；只移动其他行，避免 DOM 搬移让输入框失焦。
    for (const element of current) {
      if (!protectedElements.has(element) || !nextElements.includes(element)) continue;
      nextElements.splice(nextElements.indexOf(element), 1);
      nextElements.splice(Math.min(current.indexOf(element), nextElements.length), 0, element);
    }
    const keep = new Set(nextElements);
    for (const child of Array.from(container.children)) {
      if (!keep.has(child)) container.removeChild(child);
    }
    let reference = null;
    for (let index = nextElements.length - 1; index >= 0; index -= 1) {
      const element = nextElements[index];
      if (!protectedElements.has(element) && (element.parentElement !== container || element.nextElementSibling !== reference)) {
        container.insertBefore(element, reference);
      }
      reference = element;
    }

    if (!container.dataset.uiFocusListener) {
      container.dataset.uiFocusListener = "true";
      const flushPending = () => {
        queueMicrotask(() => {
          if (Object.hasOwn(container.dataset, "uiPendingHtml") && !protectedRowKeys(container, keyAttribute, documentObject).size) {
            const pending = container.dataset.uiPendingHtml;
            delete container.dataset.uiPendingHtml;
            patchKeyedHtml(container, pending, { keyAttribute, documentObject });
          }
        });
      };
      container.addEventListener("focusout", flushPending);
      container.addEventListener("toggle", flushPending, true);
    }
    if (protectedKeys.size && operations.some((operation) => operation.type === "defer" || operation.type === "defer-remove")) {
      container.dataset.uiPendingHtml = String(html || "");
    } else {
      delete container.dataset.uiPendingHtml;
    }
    return operations;
  }

  function maskSensitiveUrl(value) {
    const text = String(value || "");
    try {
      const url = new URL(text);
      const segments = url.pathname.split("/");
      const poolIndex = segments.findIndex((part) => part === "pool");
      const isPool = poolIndex >= 0 && segments[poolIndex + 1]
        && ["list.json", "proxies.txt", "get"].includes(segments[poolIndex + 2])
        && segments.slice(poolIndex + 3).every((part) => !part);
      if (isPool) {
        const secret = decodeURIComponent(segments[poolIndex + 1]);
        segments[poolIndex + 1] = `${secret.length > 8 ? secret.slice(0, 4) : ""}•••••`;
      }
      for (let index = 1; index < segments.length; index += 1) {
        if (/^(token|key|secret|auth|credential|password)$/i.test(decodeURIComponent(segments[index - 1] || "")) && segments[index]) {
          segments[index] = "•••••";
        }
      }
      const safeQuery = url.search.slice(1).split("&").filter(Boolean).map((pair) => {
        const separator = pair.indexOf("=");
        const rawKey = separator < 0 ? pair : pair.slice(0, separator);
        let decodedKey = rawKey;
        try { decodedKey = decodeURIComponent(rawKey.replaceAll("+", " ")); } catch (_) {}
        const publicPoolParameter = isPool && /^(strategy|format|protocol|region|q|include_starting)$/.test(decodedKey);
        return publicPoolParameter ? pair : separator < 0 ? "•••••" : `${rawKey}=•••••`;
      }).join("&");
      const lastPart = decodeURIComponent(segments.filter(Boolean).at(-1) || "");
      const safePath = isPool ? segments.join("/") : url.pathname === "/" ? "/"
        : `/•••••${lastPart.length > 6 ? `/${lastPart.slice(-6)}` : ""}`;
      return `${url.origin}${safePath}${safeQuery ? `?${safeQuery}` : ""}${url.hash ? "#•••••" : ""}`;
    } catch (_) {
      return text.length > 12 ? `${text.slice(0, 4)}•••••${text.slice(-4)}` : "•••••";
    }
  }

  const api = { planKeyedDiff, patchKeyedHtml, protectedRowKeys, maskSensitiveUrl };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.ProxyBridgeUiCore = api;
})(typeof window !== "undefined" ? window : globalThis);
