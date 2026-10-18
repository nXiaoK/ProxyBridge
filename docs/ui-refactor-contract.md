# 控制台重构接口与兼容约定

使用原生 JavaScript，保留无框架、无打包器部署。默认进入概览；自动切换默认关闭，已有无认证共享保持原样。所有管理接口继续要求实例令牌，不在 URL 中携带令牌。

## 新增接口

- `POST /api/nodes/update`：`node_id` 加可选 `region`（中文地区或两字母代码）、`favorite`（布尔）、`remark`（备注）。返回 `{ok, node}`；地区覆盖、收藏和备注刷新后保留并参与同步。
- `POST /api/subscriptions/preview`：`raw`（也接受 `content`）或 `url`，`filter_include`、`filter_exclude`（按行或逗号分隔关键词）、`filter_regex`（布尔，开启后每项按正则匹配）。返回 `{total, matched, excluded, nodes}`。包含项满足任意一项，排除项优先；空规则保持旧行为。
- 原有 `POST /api/subscriptions/import` 支持以上过滤字段，规则在自动刷新时复用。`POST /api/subscriptions/update` 接受 `id`、可选 `name` 和过滤字段；规则更改使下一次 URL 刷新重新解析。手动内容订阅可重新导入应用规则。
- `GET /api/events`：通过认证的 fetch SSE 流；`event: status` 的 data 是 `/api/status` 同形数据。连接失败退回轮询；服务器定期重连，令牌仍只放请求头。
- `POST /api/sync/preview`：只读差异预览，由 `SyncEngine.preview_pull()` 提供；实际拉取仍用原有 `/api/sync/pull`，预览不会替代冲突保护。
- `POST /api/settings/health`：`auto_switch` 布尔，默认关闭。浏览器绑定节点禁止自动切换，避免改变出口。
- `GET /api/cores`：返回 `{cores: [{core,path,version,installed,status,error}]}`；代理核心更新使用 `POST /api/cores/update {core}`。
- `GET /api/nodes/history?node_id=...`：返回 `{node_id,history}`，每节点最近 20 次测速。
- `POST /api/browser/update`：`instance_id` 或 `instance_ids` 加可选 `name`（仅单条）、`label`、`group`、`fingerprint`（部分字段即可）、`kernel_version`、`cdp_enabled`、`extensions`、`start_urls`。返回 `{ok, updated, restart_required, instance | instances}`；运行中的记录在下次启动时生效。
- `POST /api/browser/create`：`node_id`、`count`（1–50）及上述可编辑字段，只新建记录不启动。`POST /api/browser/rebind`：`instance_ids`、`node_id`，记录须已停止。`POST /api/browser/regenerate-fingerprint`：`instance_ids`。
- `POST /api/browser/open-url`：`instance_id` 加 `site`（`browserscan`、`pixelscan`、`creepjs`）或 `url`。`POST /api/browser/cookies/export`：`instance_id`、`format`（`json` / `netscape`）；`POST /api/browser/cookies/import`：`instance_id`、`content`。Cookie 接口要求本机运行且开启 CDP。
- `POST /api/browser/kernels/releases`、`default`（`id`）、`delete`（`id`）管理多版本指纹内核；`POST /api/subscriptions/delete` 新增 `delete_browser_profiles`，默认 `false` 表示保留浏览器记录与 Profile。

## 状态扩展

- `nodes` 增加 `region_code`、`region_override`、`favorite`、`remark`；`region_override` 为空表示自动识别。地区选项来自 `regions` 目录，避免前后端支持范围不一致；`region` 继续为原有中文地区，兼容网关和浏览器时区。
- `subscriptions[].usage` 是 `{upload, download, total, expire}`，单位字节和 Unix 秒；缺失字段不推断为零，不带该响应头时保留上次数据。
- `bridges[].health` 是 `{status, checked_at, error}`，status 为 unknown/ok/error；后台每 60 秒通过核心 SOCKS 端口探测现有测速目标；不会重绑已有会话。
- `smart_gateway` 增加 `allocations`（各节点累计连接数/占比）与 `recent_switches`。
- `browser_instances[].exit_check` 保存出口 IP、地区、自检时间或错误。自检经对应代理，不绕过代理，不把失败当作地区匹配成功。
- `browser_instances[]` 增加 `group`、`fingerprint`、`kernel_version`、`cdp_enabled`、`extensions`、`start_urls`，运行中另有 `cdp_port`、`cdp_ws_path`、`runtime_kernel`、`runtime_locale`；`nodes[].browser_preserved` 表示仅为浏览器记录保留的节点。状态还提供 `fingerprint_kernels`、`fingerprint_default_kernel`、`browser_extensions`、`fingerprint_check_sites` 与 `features.browser_cdp`。
- `health_settings.auto_switch` 表示是否允许无浏览器绑定节点失败后切换到同地区最快且未运行的节点。先完成核心预检，再复用原端口启动替代节点；原端口的连接会断开，后续连接使用新出口，失败时尝试恢复原节点。浏览器绑定节点始终跳过。
- `logs.system_error_total`、`system_error_24h` 和 `error_groups` 提供当前日志缓冲区的错误数量与近 24 小时 Top 5 聚合；日志时间字段继续使用 `ts`。内存日志被轮换或服务重启后，不推断缺失历史的错误数量。

## 安全与兼容

新开启共享且未显式选择认证时，生成 Token 并使用 Token 认证。生成的令牌仅在保存响应的 `generated_token` 中返回一次，常规状态只返回已配置标记；原有显式 none/basic/token 调用兼容。前端选择无认证须确认。

所有旧接口、测速取消、订阅取消、Docker 浏览器凭证和 WebDAV 冲突检查保持原有语义。敏感值默认遮蔽；显示与复制由用户显式触发。
