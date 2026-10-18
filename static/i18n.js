(function (root) {
  "use strict";

  const zhToEn = Object.freeze({
    "无延迟数据": "No latency data",
    "启动数量": "Number to Start", "启动最快节点": "Start Fastest Nodes", "启动最快节点数量": "Number of fastest nodes to start",
    "默认关闭。切换会中断连接并改变出口；浏览器绑定节点始终保持原出口。": "Off by default. Switching interrupts connections and changes the exit. Browser-bound nodes retain their exit.",
    "概览": "Overview", "工作区": "Workspace", "节点": "Nodes", "订阅": "Subscriptions",
    "运行中代理": "Running Proxies", "代理池与网关": "Pool & Gateway", "指纹浏览器": "Fingerprint Browsers",
    "日志": "Logs", "连接日志": "Connection Logs", "系统日志": "System Logs", "设置": "Settings",
    "通用": "General", "通用设置": "General", "内核管理": "Kernels", "WebDAV 同步": "WebDAV Sync", "诊断中心": "Diagnostics",
    "添加订阅": "Add Subscription", "刷新全部": "Refresh All", "全部": "All", "运行中": "Running",
    "可用": "Available", "失败": "Failed", "未测试": "Untested", "紧凑": "Compact", "标准": "Comfortable",
    "筛选": "Filter", "名称": "Name", "地区": "Region", "协议": "Protocol", "核心": "Core", "延迟": "Latency", "操作": "Actions",
    "测试可见节点": "Test Visible", "取消测速": "Cancel Test", "密度": "Density", "暂无节点": "No nodes yet",
    "没有匹配的节点": "No matching nodes", "启动": "Start", "停止": "Stop", "复制": "Copy", "更多": "More",
    "详情": "Details", "收藏": "Favorite", "测速本组": "Test Group", "启动最快": "Start Fastest",
    "访问链接": "Access Links", "轮换密钥": "Rotate Token", "智能网关": "Smart Gateway", "策略": "Strategy", "端口": "Port",
    "启动网关": "Start Gateway", "停止网关": "Stop Gateway", "暂无代理池成员": "No pool members", "健康": "Healthy",
    "内核待安装": "Kernels need installation", "核心正常": "Kernels ready", "局域网关闭": "LAN off", "同步未运行": "Sync idle",
    "浏览器": "Browser", "最近使用": "Last used", "状态": "Status", "WebDAV": "WebDAV", "查看凭证": "View Credentials",
    "本地导入": "Import Local Package", "版本": "Version", "路径": "Path", "更新": "Update", "查看日志": "View Logs",
    "外观": "Appearance", "主题": "Theme", "跟随系统": "System", "浅色": "Light", "深色": "Dark", "界面语言": "Language",
    "英语": "English", "简体中文": "Simplified Chinese", "保存设置": "Save Settings", "放弃": "Discard", "保存": "Save",
    "有未保存的更改": "Unsaved changes", "确认操作": "Confirm Action", "取消": "Cancel", "继续": "Continue",
    "已配置 · 尚未启用": "Configured · Disabled", "WebDAV 未配置": "WebDAV not configured", "同步已启用": "Sync enabled",
    "暂无测速历史。": "No speed test history.", "最近没有警告或错误": "No recent warnings or errors", "没有待处理错误": "No outstanding errors",
    "重新测速": "Retest", "导出 CSV": "Export CSV", "导出 JSON": "Export JSON", "清空": "Clear",
    "全部停止": "Stop All", "轮询": "Round Robin", "健康优先": "Health First", "延迟优先": "Lowest Latency",
    "自动化": "Automation", "端到端加密": "End-to-End Encryption", "运行诊断": "Run Diagnostics", "仅检查 WebRTC": "Check WebRTC Only",
    "错误聚合 · 近 24 小时": "Errors · Last 24 Hours", "最近事件": "Recent Events", "当前网络": "Current Network",
    "局域网共享": "LAN Sharing", "测速设置": "Speed Test Settings", "日志存储": "Log Storage", "代理健康检查": "Proxy Health",
    "连接": "Connections", "系统": "System", "暂无浏览器记录": "No browser records", "已停止": "Stopped", "正常": "Healthy",
    "启动并复制": "Start & Copy", "停止代理": "Stop Proxy", "重新测速": "Retest", "查看全部": "View all",
    "未分类": "Unclassified", "自动刷新": "Auto Refresh", "已启用": "Enabled", "未启用": "Disabled",
    "未运行": "Stopped", "当前网络": "Current Network", "已排除": "Excluded", "恢复探测": "Recovery Probe",
    "刷新": "Refresh", "删除": "Delete", "保存过滤规则": "Save Filter Rules", "节点过滤规则": "Node Filters",
    "包含关键词（每行一个）": "Include keywords (one per line)", "排除关键词（每行一个）": "Exclude keywords (one per line)",
    "按规则过滤": "Filter by Rules", "预览命中节点": "Preview Matches", "规则已更改，请重新预览。": "Rules changed. Preview again.",
    "启动后将检查出口": "Exit will be checked after launch", "地区不一致": "Region mismatch", "出口自检失败：": "Exit check failed: ",
    "无可用浏览器功能": "Browser features unavailable", "当前运行模式未提供指纹浏览器功能。": "Fingerprint browsers are unavailable in this runtime.",
    "暂无连接分配记录": "No connection allocation data", "暂无切换事件": "No recent switches", "保存并启动": "Save & Start",
    "本机代理工作台": "Local Proxy Workspace", "Docker 代理工作台": "Docker Proxy Workspace", "关闭认证": "Disable Authentication",
    "复制 SOCKS5 地址": "Copy SOCKS5 Address", "复制 HTTP/HTTPS 代理地址": "Copy HTTP/HTTPS Proxy URL", "本地端口": "Local Port",
    "监听地址": "Listen Address", "运行状态": "Runtime Status", "服务器": "Server", "备注": "Note", "收藏节点并置顶": "Favorite and Pin",
    "地区覆盖": "Region Override", "备注名": "Note", "首选核心": "Preferred Core", "首选核心失败后尝试备用核心": "Try fallback core if preferred core fails",
    "节点详情已保存": "Node details saved", "节点过滤规则": "Node Filters", "手动导入": "Manual Import", "需检查": "Needs Attention",
    "全部正常": "All Healthy", "测速正常": "Tests are healthy", "网络状态正在加载": "Loading network status", "局域网共享已关闭": "LAN sharing is off",
    "局域网共享已开启": "LAN sharing is on", "局域网": "LAN", "剩余": "Remaining", "分钟": "minutes", "小时": "hours", "天": "days",
    "搜索名称或标签": "Search names or labels", "搜索浏览器名称或标签": "Search browser names or labels",
    "成功率": "Success Rate", "健康": "Health", "平均": "Average", "新增": "Added", "删除": "Removed", "更新": "Updated",
    "导入订阅": "Import Subscription", "原始内容": "Raw Content", "开启后仅添加可信 URL。": "Only add trusted URLs when enabled.",
    "跳到主内容": "Skip to main content", "排序": "Sort", "最近测试": "Recently Tested",
    "全选所有可见节点": "Select all visible nodes", "取消选择": "Clear Selection", "测速选中": "Test Selected",
    "批量启动": "Start Selected", "批量停止": "Stop Selected", "批量开浏览器": "Open Browsers",
    "移出代理池": "Remove from Pool", "节点排序": "Sort Nodes", "搜索名称 / 服务器 / 订阅": "Search names / servers / subscriptions",
    "当前本地代理": "Local Proxies", "配置与连接": "Configuration & Connections", "网络和认证": "Network & Authentication",
    "认证方式": "Authentication", "总连接数上限": "Maximum Connections", "密码（留空保持不变）": "Password (leave blank to keep)",
    "允许的 IP / CIDR（每行一项）": "Allowed IP / CIDR (one per line)", "显示或隐藏 Token": "Show or hide token",
    "复制 Token": "Copy token", "测速设置": "Speed Test Settings", "探测目标（每行一个 HTTP/HTTPS URL）": "Probe targets (one HTTP/HTTPS URL per line)",
    "轮换方式": "Rotation", "结果过期（分钟）": "Result expiry (minutes)", "批量并发": "Batch concurrency",
    "失败重试": "Failure retries", "保存测速设置": "Save Speed Test Settings", "保留天数": "Retention (days)",
    "单文件上限（MiB）": "Maximum file size (MiB)", "总磁盘配额（MiB）": "Total disk quota (MiB)",
    "当前占用": "Current Usage", "保存日志设置": "Save Log Settings", "节点行高": "Node Row Height",
    "版本与安装状态": "Version & Installation Status", "测试连接": "Test Connection", "上传到远端": "Upload to Remote",
    "从远端拉取": "Pull from Remote", "检查远端": "Check Remote", "服务器": "Server", "订阅 URL": "Subscription URL",
    "改用原始订阅内容": "Use Raw Subscription Content", "仅包含（每行一个关键词）": "Include (one keyword per line)",
    "排除（每行一个关键词）": "Exclude (one keyword per line)", "将包含 / 排除规则作为正则表达式处理": "Treat include / exclude rules as regular expressions",
    "自动选择运行中的代理": "Automatically select a running proxy", "出口检查代理": "Exit Check Proxy",
    "包含网络检查": "Include Network Checks", "运行诊断后在此显示逐项结果": "Run diagnostics to see individual results here",
    "尚未运行": "Not Run", "消息 / 分类": "Message / Category", "级别": "Level", "全部级别": "All Levels",
    "信息": "Info", "警告": "Warning", "连接中": "Connecting", "已连接": "Connected", "已关闭": "Closed",
    "上一页": "Previous Page", "下一页": "Next Page", "日志类型": "Log Type", "允许非公网订阅地址": "Allow Non-Public Subscription URLs",
    "用于 TUN fake-IP（198.18.0.0/15）或局域网订阅。开启后仅添加可信 URL。": "For TUN fake-IP (198.18.0.0/15) or LAN subscriptions. Only trusted URLs are allowed.",
    "我的机场": "My Subscription", "用户名": "Username", "密码": "Password", "远端目录": "Remote Directory",
    "WebDAV URL": "WebDAV URL", "可选": "Optional", "已保存则留空不改": "Leave blank to keep the saved value",
    "停止运行中的浏览器": "Stop Running Browsers", "随机": "Random", "测速失败": "Speed Test Failed",
    "切换深色 / 浅色": "Toggle Dark / Light Theme", "目标 / 节点 / 客户端 / 状态": "Target / Node / Client / Status",
    "数据库存储": "Log Storage", "关闭并清除远程浏览器凭证": "Close and clear remote browser credentials",
    "关闭并清除当前远程浏览器凭证": "Close and clear current remote browser credentials", "关闭详情": "Close details",
    "重启": "Restart", "HTTP 地址": "HTTP Address", "仅端口": "Port Only", "节点详情": "Node Details",
    "浏览器记录": "Browser Records", "测速本组": "Test Group", "启动最快": "Start Fastest",
    "技术信息": "Technical Details", "绑定节点": "Bound Node", "最近刷新记录": "Recent Refresh History",
    "节点过滤规则 · 已启用": "Node Filters · Enabled", "启动中": "Starting", "停止中": "Stopping",
    "探测异常": "Health Check Failed", "上行": "Upload", "下行": "Download", "合计": "Total",
    "流量": "Traffic", "关闭": "Off", "每 15 分钟": "Every 15 Minutes", "每 30 分钟": "Every 30 Minutes",
    "每小时": "Hourly", "每 6 小时": "Every 6 Hours", "每天": "Daily", "删除订阅": "Delete Subscription",
    "复制环境变量": "Copy Environment Variables", "复制 curl 示例": "Copy curl Example", "复制 Clash 片段": "Copy Clash Snippet",
    "复制 ▾": "Copy ▾", "配置": "Configuration",
    "首选代理核心": "Preferred Proxy Core", "启动失败时会尝试备用核心": "Fallback core will be tried if launch fails",
    "收藏节点": "Favorite Node", "取消收藏": "Unfavorite", "新开浏览器": "Open Browser",
    "展开分组": "Expand Group", "折叠分组": "Collapse Group", "查看节点": "View Node",
    "更多订阅操作": "More Subscription Actions", "显示或隐藏订阅 URL": "Show or hide subscription URL",
    "保存标签": "Save Label", "标签": "Label", "保存详情": "Save Details", "自动刷新": "Auto Refresh",
    "自动识别": "Auto Detect", "查看凭证": "View Credentials", "凭证已销毁": "Credentials Destroyed",
    "打开远程链接": "Open Remote Link", "运行中": "Running", "已停止": "Stopped", "失败": "Failed",
    "浏览器": "Browser", "节点": "Nodes", "状态": "Status", "最近使用": "Last Used", "操作": "Actions",
    "运行中的代理会自动加入，可按需临时排除成员。": "Running proxies join automatically; exclude members temporarily when needed.",
    "隐藏访问链接": "Hide Access Links", "显示访问链接": "Show Access Links",
    "显示或隐藏访问链接": "Show or hide access links", "代理池": "Proxy Pool",
    "只读访问链接": "Read-only Access Link", "只读代理池访问链接": "Read-only Proxy Pool Access Link",
    "代理池链接格式": "Proxy Pool Link Format", "文本": "Text",
    "启动节点后会自动加入代理池。": "Start a node to add it to the proxy pool automatically.",
    "隐藏敏感链接": "Hide Sensitive Link", "显示完整链接": "Show Full Link",
    "启动节点后成员会自动加入，也可查看空池链接": "Start a node to add it automatically, or view the empty-pool link.",
    "密钥仅在显式显示或复制时使用。": "The key is only used when explicitly revealed or copied.",
  });
  const enToZh = (() => {
    const reverse = {};
    const ambiguous = new Set();
    for (const [zh, en] of Object.entries(zhToEn)) {
      if (ambiguous.has(en)) continue;
      if (Object.prototype.hasOwnProperty.call(reverse, en) && reverse[en] !== zh) {
        delete reverse[en];
        ambiguous.add(en);
        continue;
      }
      reverse[en] = zh;
    }
    return Object.freeze(reverse);
  })();

  // 这些选择器指向静态界面文案或动态行里的固定控件标签；不会选中记录名称、标签、日志或数据值。
  const STATIC_TEXT_SELECTORS = Object.freeze([
    ".skip-link", ".sidebar-nav .nav-group-title", ".sidebar-nav .nav-label",
    "#overviewTitle", "#nodesTitle", "#subsTitle", "#bridgesTitle", "#poolTitle", "#browsersTitle",
    "#logsTitle", "#settingsTitle", "#coresTitle", "#syncTitle", "#diagnosticsTitle", "#systemTitle",
    "[data-view='overview'] .toolbar-controls > button", "[data-view='subscriptions'] .toolbar-controls > button[data-action='open-subscription-drawer']",
    "#testVisible span", "#cancelVisibleTest span", "#nodeQuickFilters > button", "#nodeSort > option",
    "#nodeDensity > option", "#settingsNodeDensity > option", "#themeSelect > option", "#languageSelect > option",
    "#nodeViewport thead th",
    "#batchActionsBar .batch-buttons > button", "#refreshAllSubscriptions span",
    "#importTitle", "#importForm label > span", "#importForm details > summary", "#importForm button span",
    "#previewSubscriptionFilter", "#subscriptionSecurityTitle", "#subscriptionSecurityDescription",
    "#poolRotateToken", "#smartGatewayCard label > span", "#smartGatewayStrategy > option",
    "#smartGatewaySave", "#smartGatewayStop", "#browserStatusFilter > option",
    "[data-view='pool'] .toolbar-more > summary", "#poolEmptyGuide", "#poolShowLinks",
    "#poolCard .pool-access-field label > span", "#poolLinkFormats button[data-pool-format]",
    "#poolAccessLinks > p.view-hint",
    "#stopBrowsers", ".log-tab-strip [data-log-tab]", "#requestLogPrev", "#requestLogNext",
    "#systemLogPrev", "#systemLogNext", "#clearSystemLogs span",
    "#networkSettingsForm h2", "#networkSettingsForm label > span", "#networkSettingsForm select > option",
    "#networkSettingsForm .settings-toggle-copy > strong",
    "#networkSaveButton", "#speedTestSettingsForm h2", "#speedTestSettingsForm label > span",
    "#speedTestSettingsForm select > option", "#speedTestSaveButton", "#logSettingsForm h2",
    "#logSettingsForm label > span", "#logSettingsForm button", ".appearance-settings h2",
    ".appearance-settings label > span", ".appearance-settings option", ".health-settings h2",
    ".health-settings .settings-toggle-copy > strong", ".health-settings .settings-toggle-copy > span",
    "#syncCheckBtn", "#syncTestBtn", "#syncPushBtn", "#syncPullBtn", "#syncForm h2",
    "#syncForm label > span", "#syncForm .section-divider > span", "#syncForm button[type='submit']",
    "#diagnosticsWebrtcButton", "#diagnosticsRunButton", ".diagnostic-network-toggle strong",
    ".diagnostic-proxy-field > span", "#diagnosticsBridgeSelect > option:first-child", "#systemLevelFilter > option",
    "#clearLogs span",
    ".responsive-table thead th",
    ".node-row button[data-action='start']", ".node-row button[data-action='stop']",
    ".node-row .copy-menu > summary", ".node-row .copy-menu > div > button[data-action='copy']",
    ".node-row details.row-more > div > button[data-action='open-node-detail']",
    ".node-row details.row-more > div > button[data-action='test']",
    ".node-row .row-actions button[data-action='browser-records']",
    ".node-group-header .node-group-kind", ".node-group-actions button[data-action='test-node-group']",
    ".node-group-actions button[data-action='start-fastest-group']",
    ".bridge-card .status-pill", ".bridge-card .traffic-label", ".bridge-card .endpoint-label",
    ".bridge-card .bridge-actions button[data-action='stop'] span",
    ".bridge-card .bridge-actions button[data-action='restart'] span",
    ".bridge-card .bridge-actions button[data-action='copy-bridge-format']",
    ".subscription-item label.field-inline > span", ".subscription-item select.subscription-schedule > option",
    ".subscription-item button[data-action='refresh-sub'] span",
    ".subscription-item button[data-action='copy-subscription-url']",
    ".subscription-item button[data-action='delete-sub']",
    ".subscription-item button[data-action='save-subscription-filters']",
    ".subscription-item details.subscription-rules > summary",
    ".subscription-item details.subscription-history > summary",
    "#browserListRows .status-pill", "#browserListRows button[data-action='stop-browser-instance']",
    "#browserListRows button[data-action='launch-browser-instance']",
    "#browserListRows button[data-action='view-remote-browser-credentials']",
    "#browserListRows button[data-action='open-remote-browser']",
    "#nodeDetailDrawer .drawer-tabs [data-node-tab]",
    "#nodeDrawerContent .drawer-browser-row button[data-action='stop-browser-instance']",
    "#nodeDrawerContent .drawer-browser-row button[data-action='launch-browser-instance']",
    "#nodeDrawerContent .technical-details > summary", ".diagnostic-details > summary",
    "#browserRecordBody .browser-record-entry .status",
    "#browserRecordBody .browser-record-actions button[data-action='view-remote-browser-credentials'] span",
    "#browserRecordBody .browser-record-actions button[data-action='copy'] span",
    "#browserRecordBody .browser-record-actions button[data-action='open-remote-browser'] span",
    "#browserRecordBody .browser-record-actions button[data-action='stop-browser-instance'] span",
    "#browserRecordBody .browser-record-actions button[data-action='launch-browser-instance'] span",
    "#browserRecordBody .browser-record-actions button[data-action='delete-browser-instance'] span",
  ]);

  const STATIC_COUNTED_TEXT_TARGETS = Object.freeze([
    {
      selector: ".node-row .row-actions button[data-action='browser-records']",
      label: "浏览器记录",
      suffixPattern: /^\s*\d*\s*$/,
    },
    {
      selector: ".subscription-item details.subscription-history > summary",
      label: "最近刷新记录",
      suffixPattern: /^\s+·\s*\d+\s*$/,
    },
  ]);

  const STATIC_ATTRIBUTE_TARGETS = Object.freeze({
    "aria-label": Object.freeze([
      ".sidebar", ".sidebar-nav", ".nav-item", "#subscriptionFilter", "#nodeSort", "#searchInput",
      "#testVisible", "#cancelVisibleTest", "#refreshList", "#nodeDensity", "#nodeQuickFilters",
      "#nodeSelectAll", "#refreshStatus", "#allowNonPublicSubscriptions", "#browserSearchInput",
      "#logSearchInput", "#requestLogPrev", "#requestLogNext", "#allowLan", "#themeToggle",
      "#systemSearchInput", "#systemLevelFilter", "#systemLogPrev", "#systemLogNext",
      "#lanTokenField [data-action='reveal-lan-token']", "#lanTokenField [data-action='copy-lan-token']",
      "#healthAutoSwitch", "#poolCard", "#poolLinkFormats", "#poolAccessUrl", "#poolRevealLink",
      ".bridge-card button[data-action='copy']",
      ".node-row button[data-action='launch-browser']", ".browser-list-table button[data-action='open-node-detail']",
      ".subscription-item button[data-action='reveal-subscription-url']",
      ".subscription-item details.row-more > summary",
    ]),
    title: Object.freeze([
      "#themeToggle", "#refreshList", "#refreshStatus", ".node-row .favorite-toggle",
      ".node-row button[data-action='launch-browser']", ".node-group-toggle",
      "#poolShowLinks", "#poolRevealLink",
      ".bridge-card button[data-action='copy']", ".browser-list-table button[data-action='open-node-detail']",
    ]),
    placeholder: Object.freeze([
      "#searchInput", "#browserSearchInput", "#logSearchInput", "#systemSearchInput", "#syncUsername",
      "#syncPassword", "#syncEncryptPassword",
    ]),
  });

  const textSources = new WeakMap();
  const attributeSources = new WeakMap();

  function translate(text, locale = "zh-CN") {
    const value = String(text ?? "");
    if (locale === "en") return zhToEn[value] ?? value;
    return enToZh[value] ?? value;
  }

  function getLocale(storage = root.localStorage) {
    try { return storage?.getItem("pb-locale") === "en" ? "en" : "zh-CN"; }
    catch (_) { return "zh-CN"; }
  }

  function setLocale(locale, { documentObject = root.document, storage = root.localStorage } = {}) {
    const next = locale === "en" ? "en" : "zh-CN";
    try { storage?.setItem("pb-locale", next); } catch (_) {}
    if (documentObject) {
      documentObject.documentElement.lang = next === "en" ? "en" : "zh-CN";
      documentObject.documentElement.dataset.locale = next;
      apply(documentObject, next);
    }
    return next;
  }

  function translatedText(source, locale) {
    const value = String(source ?? "");
    if (!value.trim()) return value;
    const leading = value.match(/^\s*/)?.[0] || "";
    const trailing = value.match(/\s*$/)?.[0] || "";
    const core = value.slice(leading.length, value.length - trailing.length);
    return `${leading}${translate(core, locale)}${trailing}`;
  }

  function rememberAndTranslateText(node, locale) {
    const current = String(node.nodeValue ?? "");
    let entry = textSources.get(node);
    if (!entry || current !== entry.applied) entry = { source: current, applied: current };
    const next = translatedText(entry.source, locale);
    node.nodeValue = next;
    entry.applied = next;
    textSources.set(node, entry);
  }

  function rememberAndTranslateCountedLabel(element, label, suffixPattern, locale) {
    const node = [...(element.childNodes || [])].find((child) => child.nodeType === 3);
    if (!node) return;
    const current = String(node.nodeValue ?? "");
    let entry = textSources.get(node);
    if (!entry || current !== entry.applied) entry = { source: current, applied: current };
    const source = entry.source;
    if (!source.startsWith(label)) {
      rememberAndTranslateText(node, locale);
      return;
    }
    const suffix = source.slice(label.length);
    if (!suffixPattern.test(suffix)) {
      rememberAndTranslateText(node, locale);
      return;
    }
    node.nodeValue = `${translate(label, locale)}${suffix}`;
    entry.applied = node.nodeValue;
    textSources.set(node, entry);
  }

  function translateElementText(element, locale, recursive = false) {
    if (element.matches?.("script,style,code,pre,input,textarea,select,[data-i18n-ignore]")) return;
    const visit = (current) => {
      for (const child of current.childNodes || []) {
        if (child.nodeType === 3) {
          rememberAndTranslateText(child, locale);
          continue;
        }
        if (child.nodeType !== 1) continue;
        if (child.matches?.("script,style,code,pre,input,textarea,select,[data-i18n-ignore]")) continue;
        if (recursive) visit(child);
      }
    };
    visit(element);
  }

  function rememberAndTranslateAttribute(element, name, locale) {
    const current = element.getAttribute(name);
    if (current === null) return;
    let entries = attributeSources.get(element);
    if (!entries) {
      entries = new Map();
      attributeSources.set(element, entries);
    }
    let entry = entries.get(name);
    if (!entry || current !== entry.applied) entry = { source: current, applied: current };
    const next = translate(entry.source, locale);
    element.setAttribute(name, next);
    entry.applied = next;
    entries.set(name, entry);
  }

  function apply(documentObject = root.document, locale = getLocale()) {
    if (!documentObject?.querySelectorAll) return;

    for (const element of documentObject.querySelectorAll("[data-i18n]")) {
      translateElementText(element, locale, true);
    }
    for (const selector of STATIC_TEXT_SELECTORS) {
      for (const element of documentObject.querySelectorAll(selector)) {
        translateElementText(element, locale);
      }
    }
    for (const target of STATIC_COUNTED_TEXT_TARGETS) {
      for (const element of documentObject.querySelectorAll(target.selector)) {
        rememberAndTranslateCountedLabel(element, target.label, target.suffixPattern, locale);
      }
    }

    for (const name of ["aria-label", "title", "placeholder"]) {
      const marker = `[data-i18n-${name}]`;
      for (const element of documentObject.querySelectorAll(marker)) {
        rememberAndTranslateAttribute(element, name, locale);
      }
      for (const selector of STATIC_ATTRIBUTE_TARGETS[name] || []) {
        for (const element of documentObject.querySelectorAll(selector)) {
          rememberAndTranslateAttribute(element, name, locale);
        }
      }
    }
  }

  const api = {
    translate,
    getLocale,
    setLocale,
    apply,
    dictionary: zhToEn,
    staticTextSelectors: STATIC_TEXT_SELECTORS,
    staticCountedTextTargets: STATIC_COUNTED_TEXT_TARGETS,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.ProxyBridgeI18n = api;
})(typeof window !== "undefined" ? window : globalThis);
