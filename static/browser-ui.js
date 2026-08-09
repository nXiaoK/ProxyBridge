(function (root, factory) {
  const browserUi = factory(root);
  if (typeof module === "object" && module.exports) module.exports = browserUi;
  root.ProxyBridgeBrowserUi = browserUi;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  function safeRemoteBrowserUrl(value, URLConstructor = root.URL) {
    const raw = String(value || "").trim();
    if (!raw || typeof URLConstructor !== "function") return "";
    try {
      const parsed = new URLConstructor(raw);
      return parsed.protocol === "https:"
        && !parsed.username
        && !parsed.password
        && parsed.pathname === "/"
        && !parsed.search
        && !parsed.hash
        ? parsed.href
        : "";
    } catch (_) {
      return "";
    }
  }

  // CSS.escape 的兼容实现，避免持久化或同步进来的实例 ID 破坏属性选择器。
  function cssEscape(value, cssApi = root.CSS) {
    const string = String(value ?? "");
    if (cssApi && typeof cssApi.escape === "function") return cssApi.escape(string);
    const length = string.length;
    let result = "";
    const firstCodeUnit = string.charCodeAt(0);
    for (let index = 0; index < length; index += 1) {
      const codeUnit = string.charCodeAt(index);
      if (codeUnit === 0x0000) {
        result += "\uFFFD";
      } else if (
        (codeUnit >= 0x0001 && codeUnit <= 0x001f)
        || codeUnit === 0x007f
        || (index === 0 && codeUnit >= 0x0030 && codeUnit <= 0x0039)
        || (index === 1 && codeUnit >= 0x0030 && codeUnit <= 0x0039 && firstCodeUnit === 0x002d)
      ) {
        result += `\\${codeUnit.toString(16)} `;
      } else if (index === 0 && codeUnit === 0x002d && length === 1) {
        result += "\\-";
      } else if (
        codeUnit >= 0x0080
        || codeUnit === 0x002d
        || codeUnit === 0x005f
        || (codeUnit >= 0x0030 && codeUnit <= 0x0039)
        || (codeUnit >= 0x0041 && codeUnit <= 0x005a)
        || (codeUnit >= 0x0061 && codeUnit <= 0x007a)
      ) {
        result += string.charAt(index);
      } else {
        result += `\\${string.charAt(index)}`;
      }
    }
    return result;
  }

  function domClipboardCopy(text, environment = root) {
    const documentObject = environment?.document;
    if (!documentObject?.body || typeof documentObject.createElement !== "function") return false;
    const activeElement = documentObject.activeElement;
    const temporary = documentObject.createElement("textarea");
    temporary.value = String(text ?? "");
    temporary.readOnly = true;
    temporary.setAttribute?.("aria-hidden", "true");
    temporary.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    documentObject.body.appendChild(temporary);
    let copied = false;
    try {
      temporary.select();
      copied = typeof documentObject.execCommand === "function" && documentObject.execCommand("copy") === true;
    } catch (_) {
      copied = false;
    } finally {
      temporary.remove();
      if (activeElement && typeof activeElement.focus === "function") {
        try {
          activeElement.focus({ preventScroll: true });
        } catch (_) {
          activeElement.focus();
        }
      }
    }
    return copied;
  }

  async function copyText(text, environment = root) {
    const clipboard = environment?.navigator?.clipboard;
    if (clipboard && typeof clipboard.writeText === "function") {
      try {
        await clipboard.writeText(String(text ?? ""));
        return true;
      } catch (_) {
        // 局域网 HTTP 页面通常不是安全上下文，继续使用 DOM 复制回退。
      }
    }
    return domClipboardCopy(text, environment);
  }

  function credentialFields(fields) {
    return {
      remoteUrl: String(fields?.remoteUrl?.value || ""),
      username: String(fields?.username?.value || ""),
      password: String(fields?.password?.value || ""),
    };
  }

  function setCredentialFields(fields, credentials) {
    fields.remoteUrl.value = String(credentials?.remoteUrl || "");
    fields.username.value = String(credentials?.username || "");
    fields.password.value = String(credentials?.password || "");
    return credentialFields(fields);
  }

  function clearCredentialFields(fields) {
    fields.remoteUrl.value = "";
    fields.username.value = "";
    fields.password.value = "";
  }

  function setCredentialView(view, success) {
    view.setup.hidden = Boolean(success);
    view.success.hidden = !success;
    view.dialog.setAttribute(
      "aria-describedby",
      success ? "remoteBrowserLaunchSuccessDescription" : "remoteBrowserLaunchDescription",
    );
  }

  function formatCredentials(credentials) {
    return [
      `远程浏览器链接：${String(credentials?.remoteUrl || "")}`,
      `访问用户名：${String(credentials?.username || "")}`,
      `访问密码：${String(credentials?.password || "")}`,
    ].join("\n");
  }

  function browserInstanceLastUsedAt(instance) {
    return Math.max(
      Number(instance?.last_used_at || 0) || 0,
      Number(instance?.last_started_at || 0) || 0,
      Number(instance?.last_stopped_at || 0) || 0,
      Number(instance?.created_at || 0) || 0,
    );
  }

  function sortBrowserInstancesByRecentUse(instances) {
    return (Array.isArray(instances) ? instances : [])
      .slice()
      .sort((a, b) => {
        const usedDiff = browserInstanceLastUsedAt(b) - browserInstanceLastUsedAt(a);
        if (usedDiff) return usedDiff;
        const createdDiff = (Number(b?.created_at || 0) || 0) - (Number(a?.created_at || 0) || 0);
        if (createdDiff) return createdDiff;
        return String(b?.id || "").localeCompare(String(a?.id || ""));
      });
  }

  return Object.freeze({
    safeRemoteBrowserUrl,
    cssEscape,
    copyText,
    credentialFields,
    setCredentialFields,
    clearCredentialFields,
    setCredentialView,
    formatCredentials,
    browserInstanceLastUsedAt,
    sortBrowserInstancesByRecentUse,
  });
});
