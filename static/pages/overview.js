(function (root) {
  "use strict";

  function createOverviewRenderer(context) {
    const { $, appState, runningBridges, escapeHtml, formatLogTime, icon } = context;
    function renderOverview() {
      const container = $("#overviewContent");
      if (!container) return;
      const bridges = runningBridges();
      const available = appState.nodes.filter((node) => appState.tests[node.id]?.ok).length;
      const failed = appState.nodes.filter((node) => appState.tests[node.id] && !appState.tests[node.id].ok).length;
      const recent = (appState.systemLogs || []).filter((entry) => /warn|warning|error|critical|错误|警告/i.test(String(entry.level || entry.severity || ""))).slice(0, 8);
      const noSetup = !(appState.subscriptions || []).length;
      $("#overviewStatus").textContent = noSetup ? "先添加一条订阅开始使用" : `${bridges.length} 个代理运行中 · ${appState.nodes.length} 个节点`;
      if (noSetup) {
        container.innerHTML = `<section class="onboarding-card"><span class="eyebrow">开始使用</span><h2>三步建立本地代理</h2><ol class="onboarding-steps"><li><span>1</span><div><strong>添加订阅</strong><small>导入订阅 URL 或本地配置</small></div><button class="primary-button" data-action="open-subscription-drawer">添加订阅</button></li><li><span>2</span><div><strong>测试节点</strong><small>比较延迟和可用状态</small></div><button class="toolbar-button" data-action="navigate" data-view="nodes">前往节点</button></li><li><span>3</span><div><strong>启动代理</strong><small>获取本机 SOCKS5 和 HTTP 端口</small></div><button class="toolbar-button" data-action="navigate" data-view="nodes">选择节点</button></li></ol></section>`;
        return;
      }
      const subscriptionErrors = (appState.subscriptions || []).filter((item) => item.last_error).length;
      const systemErrors24h = Number(appState.logCenter?.system_error_24h ?? 0);
      const metrics = [
        ["运行中代理", bridges.length, "本机端口", "bridges"],
        ["可用节点", `${available} / ${appState.nodes.length}`, failed ? `${failed} 个测速失败` : "测速正常", "nodes"],
        ["订阅状态", `${appState.subscriptions.length} 条`, subscriptionErrors ? `${subscriptionErrors} 条需要检查` : "全部正常", "subscriptions"],
        ["近 24 小时错误", systemErrors24h, "系统日志", "system"],
      ];
      const runningList = bridges.length ? bridges.slice(0, 8).map((bridge) => `<div class="overview-proxy-row"><span class="state-dot is-ok"></span><strong>${escapeHtml(bridge.node_name || "节点")}</strong><code>:${escapeHtml(bridge.port)}</code><span>${escapeHtml(appState.tests[bridge.node_id]?.latency_ms == null ? "—" : `${appState.tests[bridge.node_id].latency_ms} ms`)}</span><button class="icon-button" data-action="copy" data-text="${escapeHtml(bridge.socks_url)}" aria-label="复制 SOCKS5 地址">${icon("copy")}</button></div>`).join("") : `<div class="compact-empty">暂无运行中的代理 <button class="toolbar-button" data-action="navigate" data-view="nodes">去节点页启动</button></div>`;
      const eventList = recent.length ? recent.map((entry) => `<button class="overview-event" data-action="open-system-log-search" data-search="${escapeHtml(entry.message || entry.category || "")}"><span class="event-dot ${/error|critical|错误/i.test(String(entry.level || entry.severity || "")) ? "is-error" : "is-warn"}"></span><span>${escapeHtml(entry.message || entry.category || "系统事件")}</span><time>${escapeHtml(formatLogTime(entry.ts))}</time></button>`).join("") : `<div class="compact-empty">最近没有警告或错误</div>`;
      const topErrors = Array.isArray(appState.logCenter?.error_groups)
        ? appState.logCenter.error_groups.slice(0, 5).map((item) => [`${item.node_name || "系统"} · ${item.category || "错误"}`, Number(item.count || 0), item.node_name || item.category || ""])
        : [];
      container.innerHTML = `<div class="overview-metrics">${metrics.map(([label, value, hint, view]) => `<button class="metric-card" data-action="navigate" data-view="${view}"><span>${label}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(hint)}</small></button>`).join("")}</div><div class="overview-grid"><section class="overview-panel"><header><h2>运行中代理</h2><button class="toolbar-button" data-action="navigate" data-view="bridges">查看全部</button></header><div class="overview-proxy-list">${runningList}</div></section><section class="overview-panel"><header><h2>最近事件</h2><button class="toolbar-button" data-action="navigate" data-view="system">系统日志</button></header><div class="overview-event-list">${eventList}</div></section></div><section class="overview-panel error-summary"><header><h2>错误聚合 · 近 24 小时</h2><button class="toolbar-button" data-action="navigate" data-view="system">查看日志</button></header>${topErrors.length ? `<div>${topErrors.map(([label, count, search]) => `<button data-action="open-system-log-search" data-search="${escapeHtml(search)}"><span>${escapeHtml(label)}</span><strong>${count}</strong></button>`).join("")}</div>` : `<p class="compact-empty">没有待处理错误</p>`}</section>`;
    }

    return renderOverview;
  }

  const api = { createOverviewRenderer };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ProxyBridgePages = { ...(root.ProxyBridgePages || {}), ...api };
})(typeof window !== "undefined" ? window : globalThis);
