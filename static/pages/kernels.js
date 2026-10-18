(function (root) {
  "use strict";

  function createKernelRenderer(context) {
    const { $, appState, browserEnabled, escapeHtml } = context;

    // 指纹内核多版本列表：设为默认、删除；「环境变量」指定的内核只读。
    function renderFingerprintVersions() {
      const kernels = Array.isArray(appState.fingerprint_kernels) ? appState.fingerprint_kernels : [];
      if (!browserEnabled()) return "";
      const pinnedCounts = new Map();
      for (const instance of appState.browser_instances || []) {
        if (instance.kernel_version) pinnedCounts.set(instance.kernel_version, (pinnedCounts.get(instance.kernel_version) || 0) + 1);
      }
      const rows = kernels.map((kernel) => {
        const pinned = pinnedCounts.get(kernel.id) || 0;
        const versionLabel = kernel.version && kernel.version !== kernel.id ? `${kernel.version}` : (kernel.label || kernel.id);
        const actions = kernel.id === "env"
          ? `<small>由 PROXY_BRIDGE_FP_CHROMIUM 指定</small>`
          : `${kernel.default ? "" : `<button class="row-button" data-action="set-default-kernel" data-id="${escapeHtml(kernel.id)}">设为默认</button>`}<button class="row-button danger" data-action="delete-fingerprint-kernel" data-id="${escapeHtml(kernel.id)}" ${pinned ? `disabled title="有 ${pinned} 条浏览器记录固定使用该版本"` : ""}>删除</button>`;
        return `<tr><td><strong>${escapeHtml(versionLabel)}</strong><small>${escapeHtml(kernel.id === "legacy" ? "早期安装" : kernel.id === "env" ? "环境变量" : kernel.source === "upload" ? "本地导入" : kernel.source === "bundled" ? "离线包" : "在线下载")}</small></td><td>${kernel.default ? `<span class="status-pill is-ok">默认</span>` : `<span class="status-pill is-idle">可用</span>`}${pinned ? `<small>${pinned} 条记录固定使用</small>` : ""}</td><td><code title="${escapeHtml(kernel.path || "")}">${escapeHtml(kernel.path || "—")}</code></td><td>${actions}</td></tr>`;
      });
      return `<section class="kernel-version-section" aria-labelledby="kernelVersionTitle"><header class="kernel-version-head"><div><h2 id="kernelVersionTitle">指纹内核版本</h2><p>新建浏览器使用默认版本；在浏览器配置中可固定某个版本，避免内核升级带来指纹变化。</p></div><div class="kernel-version-actions"><button class="row-button" data-action="download-latest-kernel">下载最新版本</button><button class="row-button" data-action="open-kernel-releases">选择版本下载</button></div></header>${rows.length ? `<div class="responsive-table"><table><thead><tr><th>版本</th><th>状态</th><th>路径</th><th>操作</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>` : `<div class="compact-empty">尚未安装指纹内核。点击「下载最新版本」或在上方「本地导入」。</div>`}</section>`;
    }

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
        const name = item.core === "fingerprint-chromium" ? "指纹 Chromium（默认版本）" : item.core;
        const installed = item.installed ?? Boolean(item.version);
        const isFingerprint = item.core === "fingerprint-chromium";
        const action = isFingerprint
          ? (installed ? "" : `<button class="row-button" data-action="download-kernel" ${!browserEnabled() ? "disabled" : ""}>下载安装</button>`)
          : `<button class="row-button" data-action="update-core" data-core="${escapeHtml(item.core)}">更新</button><button class="row-button" data-action="show-core-log" data-core="${escapeHtml(item.core)}">查看日志</button>`;
        return `<tr><td><strong>${escapeHtml(name)}</strong></td><td>${escapeHtml(item.version || "—")}</td><td><span class="status-pill ${installed ? "is-ok" : "is-idle"}">${escapeHtml(item.status || (installed ? "已就绪" : "未安装"))}</span>${item.error ? `<small class="text-error">${escapeHtml(item.error)}</small>` : ""}</td><td><code title="${escapeHtml(item.path || "")}">${escapeHtml(item.path || "—")}</code></td><td>${action}<button class="toolbar-button" data-action="show-core-upload" data-core="${escapeHtml(item.core)}">本地导入</button></td></tr>`;
      }).join("")}</tbody></table></div>${renderFingerprintVersions()}`;
    }

    return renderKernelPage;
  }

  const api = { createKernelRenderer };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ProxyBridgePages = { ...(root.ProxyBridgePages || {}), ...api };
})(typeof window !== "undefined" ? window : globalThis);
