(function (root) {
  "use strict";

  function createNodeRenderer(context) {
    const { $, appState, bridgeForNode, mostRecentBridgeForNode, browserEnabled, browserInstancesForNode, runningBrowserInstancesForNode, escapeHtml, icon, latencyCell, isNodeGroupCollapsed, groupAverageLatency, initials, filteredNodes, groupNodesBySubscription, renderSpeedTestTaskControls, updateBatchActionsBar, bindNodeCheckboxes, formatDuration, uiCore } = context;
    // 出口 IP 来自测速成功后经节点访问外部服务的结果；入口地址可能只是中转，两者不一定相同。
    function renderExitCell(node) {
      const result = appState.tests?.[node.id];
      const exit = result?.exit && typeof result.exit === "object" ? result.exit : null;
      if (!exit?.ip) {
        const hint = result?.exit_error ? `出口检测失败：${result.exit_error}` : "测速成功后显示节点真实出口 IP";
        return `<span class="exit-cell is-empty" title="${escapeHtml(hint)}">—</span>`;
      }
      const exitRegion = String(exit.region_code || "").toUpperCase();
      const nodeRegion = String(node.region_code || "").toUpperCase();
      const mismatch = Boolean(exitRegion && nodeRegion && nodeRegion !== "ZZ" && exitRegion !== nodeRegion);
      // 最近一次测速失败时，后端保留的是上次检测到的出口。
      const outdated = !result.ok;
      const details = [
        `入口 ${node.server}:${node.port}`,
        `出口 ${exit.ip}${exitRegion ? ` · ${exitRegion}` : ""}`,
        mismatch ? `出口地区与节点标注 ${nodeRegion} 不一致` : "",
        exit.age_seconds == null ? "" : `检测于 ${formatDuration(exit.age_seconds)}前`,
        outdated ? "最近一次测速失败，显示的是上次检测结果" : "",
        "点击复制出口 IP",
      ].filter(Boolean).join("\n");
      return `<button type="button" class="exit-cell ${mismatch ? "is-mismatch" : ""} ${outdated ? "is-stale" : ""}" data-action="copy" data-text="${escapeHtml(exit.ip)}" title="${escapeHtml(details)}" aria-label="复制出口 IP ${escapeHtml(exit.ip)}"><span class="exit-ip">${escapeHtml(exit.ip)}</span>${exitRegion ? `<span class="exit-region">${escapeHtml(exitRegion)}</span>` : ""}</button>`;
    }

    function renderNodeRow(node) {
      const bridge = bridgeForNode(node.id);
      const previousBridge = !bridge ? mostRecentBridgeForNode(node.id) : null;
      const browserInstances = browserEnabled() ? browserInstancesForNode(node.id) : [];
      const server = `${node.server}:${node.port}`;
      const isSelected = appState.selectedNodeIds.has(node.id);
      const healthError = bridge?.health?.status === "error" || (!bridge && previousBridge?.health?.status === "error");
      const regionCode = String(node.region_code || node.region || "UN").slice(0, 2).toUpperCase();
      const coreLabel = node.preferred_core || (node.core_options || ["sing-box"])[0] || "sing-box";
      const runningBrowserCount = browserEnabled() ? runningBrowserInstancesForNode(node.id).length : 0;
      const favorite = Boolean(node.favorite);
      const subtitle = bridge ? `:${bridge.port} · ${healthError ? "探测异常" : "运行中"}` : healthError ? `上次退出异常 · ${previousBridge.health.error || "查看系统日志"}` : node.browser_preserved ? "已从订阅移除 · 仅为浏览器记录保留" : (node.remark || server);
      return `
        <tr class="node-row ${bridge ? "is-running" : ""} ${healthError ? "is-unhealthy" : ""} ${isSelected ? "is-selected" : ""}" data-ui-key="node:${escapeHtml(node.id)}" data-node-id="${escapeHtml(node.id)}">
          <td class="checkbox-col">
            <label class="checkbox-wrapper" title="${isSelected ? "取消选择" : "选择"} ${escapeHtml(node.name)}">
              <input type="checkbox" class="node-checkbox" data-node-id="${escapeHtml(node.id)}" ${isSelected ? "checked" : ""} aria-label="选择 ${escapeHtml(node.name)}">
              <span class="checkbox-custom" aria-hidden="true"></span>
            </label>
          </td>
          <td>
            <div class="name-cell">
              <button type="button" class="node-name" data-action="open-node-detail" data-node="${escapeHtml(node.id)}">${escapeHtml(node.name)}</button>
              <button class="favorite-toggle ${favorite ? "is-favorite" : ""}" type="button" data-action="toggle-node-favorite" data-node="${escapeHtml(node.id)}" aria-label="${favorite ? "取消收藏" : "收藏"} ${escapeHtml(node.name)}" title="${favorite ? "取消收藏" : "收藏节点"}">★</button>
              <span class="node-source ${healthError ? "text-error" : ""}" title="${escapeHtml(subtitle)}">${escapeHtml(subtitle)}</span>
            </div>
          </td>
          <td><span class="region-cell" title="${escapeHtml(node.region || "未分类")}">${escapeHtml(regionCode)}</span></td>
          <td>${renderExitCell(node)}</td>
          <td><span class="tag protocol-tag" title="${escapeHtml([node.protocol, node.transport, node.security].filter(Boolean).join(" · "))}">${escapeHtml([node.protocol, node.transport, node.security].filter(Boolean).join(" · "))}</span></td>
          <td><span class="tag core-tag" title="${node.fallback_enabled ? "启动失败时会尝试备用核心" : "首选代理核心"}">${escapeHtml(coreLabel)}${node.fallback_enabled ? " ↗" : ""}</span></td>
          <td>${latencyCell(node.id)}</td>
          <td>
            <div class="row-actions">
              ${bridge
                ? `<details class="copy-menu"><summary class="row-button">复制 ▾</summary><div><button type="button" data-action="copy" data-text="${escapeHtml(bridge.socks_url)}">SOCKS5 地址</button><button type="button" data-action="copy" data-text="${escapeHtml(bridge.http_url || bridge.socks_url)}">HTTP 地址</button><button type="button" data-action="copy" data-text="${escapeHtml(bridge.port)}">仅端口</button></div></details><button class="row-button danger" data-action="stop" data-id="${escapeHtml(bridge.id)}">停止</button>`
                : `<button class="row-button" data-action="start" data-node="${escapeHtml(node.id)}">启动</button>`}
              ${browserEnabled() ? `<button class="icon-button" data-action="launch-browser" data-node="${escapeHtml(node.id)}" title="新开浏览器" aria-label="新开浏览器">${icon("browser")}${runningBrowserCount ? `<span class="button-count">${runningBrowserCount}</span>` : ""}</button>` : ""}
              <details class="row-more"><summary class="icon-button" aria-label="更多操作">•••</summary><div><button type="button" data-action="open-node-detail" data-node="${escapeHtml(node.id)}">节点详情</button><button type="button" data-action="browser-records" data-node="${escapeHtml(node.id)}">浏览器记录 ${browserInstances.length || ""}</button><button type="button" data-action="test" data-node="${escapeHtml(node.id)}">重新测速</button></div></details>
            </div>
          </td>
        </tr>
      `;
    }

    function renderNodeGroupHeader(group, { forceExpand = false } = {}) {
      const collapsed = !forceExpand && isNodeGroupCollapsed(group.id);
      const runningCount = group.nodes.filter((node) => bridgeForNode(node.id)).length;
      const kindLabel = group.kind === "subscription" ? "订阅" : "来源";
      const metaBits = [];
      if (group.subscription?.url) metaBits.push("URL 订阅");
      else if (group.kind === "source") metaBits.push("手动/未挂接");
      const meta = metaBits.join(" · ");
      return `
        <tr class="node-group-header ${collapsed ? "is-collapsed" : ""}" data-ui-key="group:${escapeHtml(group.id)}" data-group-id="${escapeHtml(group.id)}">
          <td colspan="8">
            <button
              type="button"
              class="node-group-toggle"
              data-action="toggle-node-group"
              data-group="${escapeHtml(group.id)}"
              aria-expanded="${collapsed ? "false" : "true"}"
              title="${collapsed ? "展开分组" : "折叠分组"}"
            >
              <span class="node-group-chevron" aria-hidden="true">${icon("chevron")}</span>
              <span class="node-group-avatar" aria-hidden="true">${escapeHtml(initials(group.name))}</span>
              <span class="node-group-text">
                <span class="node-group-title-row">
                  <strong class="node-group-name">${escapeHtml(group.name)}</strong>
                  <span class="tag node-group-kind">${escapeHtml(kindLabel)}</span>
                </span>
                <span class="node-group-meta">${group.nodes.length} 节点 · ${runningCount} 运行 · 平均 ${escapeHtml(groupAverageLatency(group.nodes))}${meta ? ` · ${escapeHtml(meta)}` : ""}</span>
              </span>
            </button>
            <span class="node-group-actions"><button type="button" class="row-button" data-action="test-node-group" data-group="${escapeHtml(group.id)}">测速本组</button><button type="button" class="row-button" data-action="start-fastest-group" data-group="${escapeHtml(group.id)}">启动最快</button></span>
          </td>
        </tr>
      `;
    }

    function renderNodes() {
      const rows = $("#nodeRows");
      const empty = $("#emptyNodes");
      const nodes = filteredNodes();
      const groups = groupNodesBySubscription(nodes);
      renderSpeedTestTaskControls();
      const groupCount = groups.length;
      $("#nodeCount").textContent =
        groupCount > 1
          ? `${nodes.length} / ${appState.nodes.length} 个节点 · ${groupCount} 组`
          : `${nodes.length} / ${appState.nodes.length} 个节点`;
      empty.classList.toggle("visible", appState.nodes.length === 0);
      if (!nodes.length) {
        uiCore.patchKeyedHtml(rows, "", { keyAttribute: "data-ui-key" });
        if (appState.nodes.length) {
          empty.textContent = "没有匹配的节点";
          empty.classList.add("visible");
        } else {
          empty.textContent = "暂无节点";
        }
        updateBatchActionsBar();
        return;
      }
      empty.classList.remove("visible");

      // 搜索时强制展开全部分组，避免命中节点被藏在折叠里。
      const forceExpand = Boolean(appState.filters.search.trim());
      const densityFallback = document.documentElement.dataset.nodeDensity === "standard" ? 44 : 36;
      const nodeRowHeight = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--node-row-height")) || densityFallback;
      const groupRowHeight = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--node-group-row-height")) || 40;
      const flattened = groups.flatMap((group) => {
        const collapsed = !forceExpand && isNodeGroupCollapsed(group.id);
        return [
          { key: `group:${group.id}`, height: groupRowHeight, html: renderNodeGroupHeader(group, { forceExpand }) },
          ...(collapsed ? [] : group.nodes.map((node) => ({ key: `node:${node.id}`, height: nodeRowHeight, html: renderNodeRow(node) }))),
        ];
      });
      const viewport = $("#nodeViewport");
      let visibleItems = flattened;
      if (appState.nodes.length > 300 && viewport) {
        const topTarget = Math.max(0, viewport.scrollTop - viewport.clientHeight * 0.4);
        let start = 0;
        let offset = 0;
        while (start < flattened.length && offset + flattened[start].height < topTarget) offset += flattened[start++].height;
        const endTarget = viewport.scrollTop + Math.max(viewport.clientHeight, 600);
        let end = start;
        let visibleHeight = offset;
        while (end < flattened.length && visibleHeight < endTarget + 400) visibleHeight += flattened[end++].height;
        const topHeight = flattened.slice(0, start).reduce((sum, item) => sum + item.height, 0);
        const bottomHeight = flattened.slice(end).reduce((sum, item) => sum + item.height, 0);
        visibleItems = [
          ...(topHeight ? [{ key: "virtual:top", height: topHeight, html: `<tr data-ui-key="virtual:top" class="virtual-spacer"><td colspan="8" style="height:${topHeight}px"></td></tr>` }] : []),
          ...flattened.slice(start, end),
          ...(bottomHeight ? [{ key: "virtual:bottom", height: bottomHeight, html: `<tr data-ui-key="virtual:bottom" class="virtual-spacer"><td colspan="8" style="height:${bottomHeight}px"></td></tr>` }] : []),
        ];
      }
      uiCore.patchKeyedHtml(rows, visibleItems.map((item) => item.html).join(""), { keyAttribute: "data-ui-key" });

      // 更新批量操作条的可见性
      updateBatchActionsBar();
      // 绑定复选框事件
      bindNodeCheckboxes();
    }

    return renderNodes;
  }

  const api = { createNodeRenderer };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ProxyBridgePages = { ...(root.ProxyBridgePages || {}), ...api };
})(typeof window !== "undefined" ? window : globalThis);
