(function (root) {
  "use strict";

  function createBrowserRenderer(context) {
    const { $, appState, browserPageFilter, browserUi, browserEnabled, remoteBrowserEnabled, escapeHtml, nodeById, safeRemoteBrowserUrl, formatRelativeTime, browserStatusLabel, uiCore } = context;
    let browserNodeFilterOptionsSignature = "";

    function renderBrowserPage() {
      const list = $("#browserList");
      if (!list) return;
      const nodeFilter = $("#browserNodeFilter");
      if (nodeFilter) {
        const nextSignature = appState.nodes.map((node) => `${node.id}\u0000${node.name}`).join("\u0001");
        if (nextSignature !== browserNodeFilterOptionsSignature) {
          if (document.activeElement === nodeFilter) {
            if (nodeFilter.dataset.refreshOptionsOnBlur !== "true") {
              nodeFilter.dataset.refreshOptionsOnBlur = "true";
              nodeFilter.addEventListener("blur", () => requestAnimationFrame(() => {
                delete nodeFilter.dataset.refreshOptionsOnBlur;
                renderBrowserPage();
              }), { once: true });
            }
          } else {
            const selected = browserPageFilter.node;
            nodeFilter.innerHTML = `<option value="all">全部节点</option>${appState.nodes.map((node) => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.name)}</option>`).join("")}`;
            nodeFilter.value = appState.nodes.some((node) => node.id === selected) ? selected : "all";
            browserPageFilter.node = nodeFilter.value;
            browserNodeFilterOptionsSignature = nextSignature;
          }
        } else if (document.activeElement !== nodeFilter) {
          nodeFilter.value = browserPageFilter.node;
        }
      }
      const records = browserUi.sortBrowserInstancesByRecentUse(appState.browser_instances || []).filter((instance) => {
        if (browserPageFilter.node !== "all" && instance.node_id !== browserPageFilter.node) return false;
        const running = ["running", "starting", "stopping"].includes(instance.status);
        if (browserPageFilter.status === "running" && !running) return false;
        if (browserPageFilter.status === "stopped" && running) return false;
        const query = String(browserPageFilter.search || "").trim().toLowerCase();
        if (query) {
          const node = nodeById(instance.node_id);
          if (![instance.label, instance.name, instance.node_name, node?.name].some((value) => String(value || "").toLowerCase().includes(query))) return false;
        }
        return true;
      });
      $("#browserCount").textContent = `${records.length} 条记录`;
      $("#stopBrowsers").hidden = !records.some((record) => ["running", "starting", "stopping"].includes(record.status));
      if (!browserEnabled()) {
        list.innerHTML = `<div class="compact-empty">当前运行模式未提供指纹浏览器功能。</div>`;
        return;
      }
      if (!list.querySelector("#browserListRows")) {
        list.innerHTML = `<div class="responsive-table"><table><thead><tr><th>浏览器</th><th>绑定节点</th><th>状态</th><th>最近使用</th><th>WebDAV</th><th>操作</th></tr></thead><tbody id="browserListRows"></tbody></table></div>`;
      }
      const rows = records.map((instance) => {
        const running = ["running", "starting", "stopping"].includes(instance.status);
        const node = nodeById(instance.node_id);
        const label = instance.label || instance.name || "浏览器";
        const exit = instance.exit_check || {};
        const expectedRegion = String(node?.region_code || "").toUpperCase();
        const exitRegion = String(exit.region_code || exit.region || "").toUpperCase();
        const exitMismatch = Boolean(exitRegion && expectedRegion && expectedRegion !== "ZZ" && exitRegion !== expectedRegion);
        const exitLabel = exit.error ? `出口自检失败：${exit.error}` : exit.ip ? `${exit.ip}${exitRegion ? ` · ${exitRegion}` : ""}${exitMismatch ? " · 地区不一致" : ""}` : "启动后将检查出口";
        const dockerActions = remoteBrowserEnabled() ? `<button class="row-button" data-action="view-remote-browser-credentials" data-instance="${escapeHtml(instance.id)}" ${running ? "" : "disabled"}>查看凭证</button>${safeRemoteBrowserUrl(instance.remote_url) ? `<button class="row-button" data-action="open-remote-browser" data-instance="${escapeHtml(instance.id)}">打开远程链接</button>` : ""}` : "";
        return `<tr data-ui-key="browser:${escapeHtml(instance.id)}"><td><strong>${escapeHtml(label)}</strong><small>${escapeHtml(instance.name || "独立指纹")}</small></td><td>${escapeHtml(node?.name || instance.node_name || "已删除节点")}</td><td><span class="status-pill ${running ? "is-ok" : instance.status === "failed" ? "is-error" : "is-idle"}">${escapeHtml(browserStatusLabel(instance.status))}</span></td><td>${escapeHtml(formatRelativeTime(browserUi.browserInstanceLastUsedAt(instance)))}</td><td><span>${escapeHtml(instance.sync_status || (appState.sync?.enabled ? "待同步" : "未启用"))}</span><small class="${exit.error || exitMismatch ? "text-warning" : ""}" title="${escapeHtml(exit.error || "")}">${escapeHtml(exitLabel)}</small></td><td>${running ? `<button class="row-button danger" data-action="stop-browser-instance" data-node="${escapeHtml(instance.node_id)}" data-instance="${escapeHtml(instance.id)}">停止</button>` : `<button class="row-button" data-action="launch-browser-instance" data-node="${escapeHtml(instance.node_id)}" data-instance="${escapeHtml(instance.id)}">启动</button>`}${dockerActions}<button class="icon-button" data-action="open-node-detail" data-node="${escapeHtml(instance.node_id)}" title="查看节点">⋯</button></td></tr>`;
      });
      const markup = rows.length
        ? rows.join("")
        : `<tr class="browser-list-empty-row" data-ui-key="browser:empty"><td colspan="6"><div class="compact-empty">没有匹配的浏览器记录。可从节点详情新开独立指纹浏览器。</div></td></tr>`;
      uiCore.patchKeyedHtml($("#browserListRows"), markup, { keyAttribute: "data-ui-key" });
    }

    return renderBrowserPage;
  }

  const api = { createBrowserRenderer };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ProxyBridgePages = { ...(root.ProxyBridgePages || {}), ...api };
})(typeof window !== "undefined" ? window : globalThis);
