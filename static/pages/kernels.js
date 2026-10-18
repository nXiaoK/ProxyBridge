(function (root) {
  "use strict";

  function createKernelRenderer(context) {
    const { $, appState, browserEnabled, escapeHtml } = context;
    function renderKernelPage() {
      const list = $("#kernelList");
      if (!list) return;
      const catalog = Array.isArray(appState.coreCatalog) ? appState.coreCatalog : [];
      const items = catalog.length ? catalog : [
        { core: "sing-box", version: "", installed: Boolean(appState.cores["sing-box"]), path: appState.cores["sing-box"] || "" },
        { core: "xray", version: "", installed: Boolean(appState.cores.xray), path: appState.cores.xray || "" },
        { core: "fingerprint-chromium", version: "", installed: Boolean(appState.fingerprint_chromium), path: appState.fingerprint_chromium || "" },
      ];
      list.innerHTML = `<div class="responsive-table"><table><thead><tr><th>内核</th><th>版本</th><th>状态</th><th>路径</th><th>操作</th></tr></thead><tbody>${items.map((item) => {
        const name = item.core === "fingerprint-chromium" ? "指纹 Chromium" : item.core;
        const installed = item.installed ?? Boolean(item.version);
        const isFingerprint = item.core === "fingerprint-chromium";
        const action = isFingerprint
          ? `<button class="row-button" data-action="download-kernel" ${!browserEnabled() ? "disabled" : ""}>${installed ? "重新下载" : "下载安装"}</button>`
          : `<button class="row-button" data-action="update-core" data-core="${escapeHtml(item.core)}">更新</button><button class="row-button" data-action="show-core-log" data-core="${escapeHtml(item.core)}">查看日志</button>`;
        return `<tr><td><strong>${escapeHtml(name)}</strong></td><td>${escapeHtml(item.version || "—")}</td><td><span class="status-pill ${installed ? "is-ok" : "is-idle"}">${escapeHtml(item.status || (installed ? "已就绪" : "未安装"))}</span>${item.error ? `<small class="text-error">${escapeHtml(item.error)}</small>` : ""}</td><td><code title="${escapeHtml(item.path || "")}">${escapeHtml(item.path || "—")}</code></td><td>${action}<button class="toolbar-button" data-action="show-core-upload" data-core="${escapeHtml(item.core)}">本地导入</button></td></tr>`;
      }).join("")}</tbody></table></div>`;
    }

    return renderKernelPage;
  }

  const api = { createKernelRenderer };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ProxyBridgePages = { ...(root.ProxyBridgePages || {}), ...api };
})(typeof window !== "undefined" ? window : globalThis);
