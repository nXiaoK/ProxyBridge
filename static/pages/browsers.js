(function (root) {
  "use strict";

  function createBrowserRenderer(context) {
    const { $, appState, browserPageFilter, browserUi, browserConfig, browserEnabled, remoteBrowserEnabled, escapeHtml, nodeById, safeRemoteBrowserUrl, formatRelativeTime, browserStatusLabel, uiCore } = context;
    let browserNodeFilterOptionsSignature = "";
    let browserGroupFilterOptionsSignature = "";

    const isFocused = (element) => typeof document !== "undefined" && document.activeElement === element;

    // 下拉框获得焦点时不重建选项，失焦后再按最新数据刷新，避免用户正在选择时被打断。
    function refreshSelectOptions(select, signature, previousSignature, buildOptions, filterKey) {
      if (!select) return previousSignature;
      if (signature === previousSignature) {
        if (!isFocused(select)) select.value = browserPageFilter[filterKey];
        return previousSignature;
      }
      if (isFocused(select)) {
        if (select.dataset.refreshOptionsOnBlur !== "true") {
          select.dataset.refreshOptionsOnBlur = "true";
          select.addEventListener("blur", () => requestAnimationFrame(() => {
            delete select.dataset.refreshOptionsOnBlur;
            renderBrowserPage();
          }), { once: true });
        }
        return previousSignature;
      }
      const selected = browserPageFilter[filterKey];
      const { markup, values } = buildOptions();
      select.innerHTML = markup;
      select.value = values.includes(selected) ? selected : "all";
      browserPageFilter[filterKey] = select.value;
      return signature;
    }

    function renderBrowserPage() {
      const list = $("#browserList");
      if (!list) return;
      browserNodeFilterOptionsSignature = refreshSelectOptions(
        $("#browserNodeFilter"),
        appState.nodes.map((node) => `${node.id}\u0000${node.name}`).join("\u0001"),
        browserNodeFilterOptionsSignature,
        () => ({
          markup: `<option value="all">全部节点</option>${appState.nodes.map((node) => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.name)}${node.browser_preserved ? "（已移除）" : ""}</option>`).join("")}`,
          values: ["all", ...appState.nodes.map((node) => node.id)],
        }),
        "node",
      );
      const groups = browserConfig.browserGroups(appState.browser_instances || []);
      browserGroupFilterOptionsSignature = refreshSelectOptions(
        $("#browserGroupFilter"),
        groups.join("\u0001"),
        browserGroupFilterOptionsSignature,
        () => ({
          markup: `<option value="all">全部分组</option><option value="__none__">未分组</option>${groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("")}`,
          values: ["all", "__none__", ...groups],
        }),
        "group",
      );
      const sorted = browserUi.sortBrowserInstancesByRecentUse(appState.browser_instances || []);
      const records = browserConfig.filterBrowserRecords(sorted, browserPageFilter, nodeById);
      const selected = appState.selectedBrowserIds || new Set();
      const knownIds = new Set(sorted.map((item) => item.id));
      for (const id of [...selected]) if (!knownIds.has(id)) selected.delete(id);
      $("#browserCount").textContent = `${records.length} 条记录`;
      $("#stopBrowsers").hidden = !records.some((record) => browserConfig.isActive(record));
      const createButton = $("#createBrowserButton");
      if (createButton) createButton.hidden = !browserEnabled();
      const batchBar = $("#browserBatchBar");
      if (batchBar) {
        batchBar.hidden = !selected.size;
        $("#browserBatchCount").textContent = `已选择 ${selected.size} 条记录`;
      }
      if (!browserEnabled()) {
        list.innerHTML = `<div class="compact-empty">当前运行模式未提供指纹浏览器功能。</div>`;
        return;
      }
      if (!list.querySelector("#browserListRows")) {
        list.innerHTML = `<div class="responsive-table"><table><thead><tr><th class="checkbox-col"><label class="checkbox-wrapper" title="选择当前筛选的全部记录"><input type="checkbox" id="browserSelectAll" aria-label="选择当前筛选的全部浏览器记录"><span class="checkbox-custom" aria-hidden="true"></span></label></th><th>浏览器</th><th>绑定节点</th><th>指纹</th><th>状态</th><th>最近使用</th><th>WebDAV / 出口</th><th>操作</th></tr></thead><tbody id="browserListRows"></tbody></table></div>`;
      }
      const selectAll = $("#browserSelectAll");
      if (selectAll) {
        const selectedVisible = records.filter((record) => selected.has(record.id)).length;
        selectAll.checked = Boolean(records.length) && selectedVisible === records.length;
        selectAll.indeterminate = selectedVisible > 0 && selectedVisible < records.length;
      }
      const rows = records.map((instance) => {
        const running = browserConfig.isActive(instance);
        const node = nodeById(instance.node_id);
        const label = instance.label || instance.name || "浏览器";
        const exit = instance.exit_check || {};
        const expectedRegion = String(node?.region_code || "").toUpperCase();
        const exitRegion = String(exit.region_code || exit.region || "").toUpperCase();
        const exitMismatch = Boolean(exitRegion && expectedRegion && expectedRegion !== "ZZ" && exitRegion !== expectedRegion);
        const exitLabel = exit.error ? `出口自检失败：${exit.error}` : exit.ip ? `${exit.ip}${exitRegion ? ` · ${exitRegion}` : ""}${exitMismatch ? " · 地区不一致" : ""}` : "启动后将检查出口";
        const dockerActions = remoteBrowserEnabled() ? `<button class="row-button" data-action="view-remote-browser-credentials" data-instance="${escapeHtml(instance.id)}" ${running ? "" : "disabled"}>查看凭证</button>${safeRemoteBrowserUrl(instance.remote_url) ? `<button class="row-button" data-action="open-remote-browser" data-instance="${escapeHtml(instance.id)}">打开远程链接</button>` : ""}` : "";
        const isSelected = selected.has(instance.id);
        const locale = instance.runtime_locale && running ? instance.runtime_locale : null;
        const fingerprintDetail = locale?.timezone ? `${locale.timezone} · ${locale.language || ""}` : `种子 ${instance.fingerprint_seed || "-"}`;
        const nodeName = node?.name || instance.node_name || "已删除节点";
        const nodeNote = node?.browser_preserved ? `<small class="text-warning">已从订阅移除，请换绑</small>` : "";
        return `<tr class="${isSelected ? "is-selected" : ""}" data-ui-key="browser:${escapeHtml(instance.id)}"><td class="checkbox-col"><label class="checkbox-wrapper" title="${isSelected ? "取消选择" : "选择"} ${escapeHtml(label)}"><input type="checkbox" class="browser-checkbox" data-instance="${escapeHtml(instance.id)}" ${isSelected ? "checked" : ""} aria-label="选择 ${escapeHtml(label)}"><span class="checkbox-custom" aria-hidden="true"></span></label></td><td><strong>${escapeHtml(label)}</strong><small>${escapeHtml([instance.group, instance.label ? instance.name : ""].filter(Boolean).join(" · ") || "独立指纹")}</small></td><td><span>${escapeHtml(nodeName)}</span>${nodeNote}</td><td><span>${escapeHtml(browserConfig.fingerprintSummary(instance))}</span><small title="${escapeHtml(fingerprintDetail)}">${escapeHtml(fingerprintDetail)}</small></td><td><span class="status-pill ${running ? "is-ok" : instance.status === "failed" ? "is-error" : "is-idle"}">${escapeHtml(browserStatusLabel(instance.status))}</span></td><td>${escapeHtml(formatRelativeTime(browserUi.browserInstanceLastUsedAt(instance)))}</td><td><span>${escapeHtml(instance.sync_status || (appState.sync?.enabled ? "待同步" : "未启用"))}</span><small class="${exit.error || exitMismatch ? "text-warning" : ""}" title="${escapeHtml(exit.error || "")}">${escapeHtml(exitLabel)}</small></td><td>${running ? `<button class="row-button danger" data-action="stop-browser-instance" data-node="${escapeHtml(instance.node_id)}" data-instance="${escapeHtml(instance.id)}">停止</button>` : `<button class="row-button" data-action="launch-browser-instance" data-node="${escapeHtml(instance.node_id)}" data-instance="${escapeHtml(instance.id)}" ${node?.browser_preserved ? "title=\"节点已从订阅移除，启动可能失败\"" : ""}>启动</button>`}<button class="row-button" data-action="open-browser-config" data-instance="${escapeHtml(instance.id)}">配置</button>${dockerActions}<button class="icon-button" data-action="open-node-detail" data-node="${escapeHtml(instance.node_id)}" title="查看节点" aria-label="查看 ${escapeHtml(nodeName)} 节点详情">⋯</button></td></tr>`;
      });
      const markup = rows.length
        ? rows.join("")
        : `<tr class="browser-list-empty-row" data-ui-key="browser:empty"><td colspan="8"><div class="compact-empty">没有匹配的浏览器记录。点击「新建浏览器」为节点创建独立指纹环境。</div></td></tr>`;
      uiCore.patchKeyedHtml($("#browserListRows"), markup, { keyAttribute: "data-ui-key" });
    }

    return renderBrowserPage;
  }

  const api = { createBrowserRenderer };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ProxyBridgePages = { ...(root.ProxyBridgePages || {}), ...api };
})(typeof window !== "undefined" ? window : globalThis);
