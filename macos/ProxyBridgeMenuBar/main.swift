import AppKit
import Darwin
import Foundation
import WebKit

struct StatusPayload: Decodable {
    let cores: [String: String?]
    let subscriptions: [Subscription]
    let nodes: [ProxyNode]
    let bridges: [Bridge]
    let tests: [String: NodeTestResult]?
    let version: String
}

struct Subscription: Decodable {
    let id: String
    let name: String
    let url: String?
    let lastUpdate: Int?
    let nodeCount: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case url
        case lastUpdate = "last_update"
        case nodeCount = "node_count"
    }
}

struct ProxyNode: Decodable {
    let id: String
    let name: String
    let `protocol`: String
    let server: String
    let port: Int
    let region: String
    let source: String?
    let transport: String?
    let security: String?
    let preferredCore: String?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case `protocol`
        case server
        case port
        case region
        case source
        case transport
        case security
        case preferredCore = "preferred_core"
    }
}

struct Bridge: Decodable {
    let id: String
    let nodeId: String
    let nodeName: String
    let `protocol`: String
    let region: String
    let core: String
    let port: Int
    let socksURL: String
    let status: String

    enum CodingKeys: String, CodingKey {
        case id
        case nodeId = "node_id"
        case nodeName = "node_name"
        case `protocol`
        case region
        case core
        case port
        case socksURL = "socks_url"
        case status
    }
}

struct NodeTestResult: Decodable {
    let ok: Bool
    let nodeId: String
    let core: String?
    let latencyMS: Int?
    let statusCode: Int?
    let target: String?
    let message: String?
    let testedAt: Int?

    enum CodingKeys: String, CodingKey {
        case ok
        case nodeId = "node_id"
        case core
        case latencyMS = "latency_ms"
        case statusCode = "status_code"
        case target
        case message
        case testedAt = "tested_at"
    }
}

struct APIError: Decodable {
    let error: String
}

final class BridgeAPI {
    let baseURL: URL

    init(port: Int) {
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    func getStatus(completion: @escaping (Result<StatusPayload, Error>) -> Void) {
        request(path: "/api/status", method: "GET", body: nil, completion: completion)
    }

    func importSubscription(name: String, url: String, raw: String, completion: @escaping (Result<StatusPayload, Error>) -> Void) {
        post(path: "/api/subscriptions/import", body: ["name": name, "url": url, "raw": raw]) { [weak self] result in
            switch result {
            case .success:
                self?.getStatus(completion: completion)
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func refreshSubscription(id: String, completion: @escaping (Result<StatusPayload, Error>) -> Void) {
        post(path: "/api/subscriptions/refresh", body: ["id": id]) { [weak self] result in
            switch result {
            case .success:
                self?.getStatus(completion: completion)
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func startBridge(nodeId: String, completion: @escaping (Result<Bridge, Error>) -> Void) {
        request(path: "/api/bridges/start", method: "POST", body: ["node_id": nodeId, "core": "auto"], completion: completion)
    }

    func testNode(nodeId: String, completion: @escaping (Result<NodeTestResult, Error>) -> Void) {
        request(path: "/api/nodes/test", method: "POST", body: ["node_id": nodeId, "core": "auto"], completion: completion)
    }

    func stopBridge(id: String, completion: @escaping (Result<Bool, Error>) -> Void) {
        post(path: "/api/bridges/stop", body: ["id": id], completion: completion)
    }

    func stopAll(completion: @escaping (Result<Bool, Error>) -> Void) {
        post(path: "/api/bridges/stop-all", body: [:], completion: completion)
    }

    private func post(path: String, body: [String: Any], completion: @escaping (Result<Bool, Error>) -> Void) {
        request(path: path, method: "POST", body: body) { (result: Result<GenericOK, Error>) in
            switch result {
            case .success:
                completion(.success(true))
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    private func request<T: Decodable>(path: String, method: String, body: [String: Any]?, completion: @escaping (Result<T, Error>) -> Void) {
        let cleanPath = path.hasPrefix("/") ? String(path.dropFirst()) : path
        var request = URLRequest(url: baseURL.appendingPathComponent(cleanPath))
        request.httpMethod = method
        request.timeoutInterval = 25
        if let body {
            request.addValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }

        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(error))
                return
            }
            guard let data else {
                completion(.failure(NSError(domain: "ProxyBridge", code: -1, userInfo: [NSLocalizedDescriptionKey: "没有收到后端响应"])))
                return
            }
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                if let apiError = try? JSONDecoder().decode(APIError.self, from: data) {
                    completion(.failure(NSError(domain: "ProxyBridge", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: apiError.error])))
                } else {
                    completion(.failure(NSError(domain: "ProxyBridge", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: "请求失败：HTTP \(http.statusCode)"])))
                }
                return
            }
            do {
                let decoded = try JSONDecoder().decode(T.self, from: data)
                completion(.success(decoded))
            } catch {
                completion(.failure(error))
            }
        }.resume()
    }
}

struct GenericOK: Decodable {
    let ok: Bool?
}

final class ConsoleWindowController: NSWindowController, WKNavigationDelegate {
    private let webView: WKWebView
    private let backendURL: URL

    init(backendURL: URL) {
        self.backendURL = backendURL
        let configuration = WKWebViewConfiguration()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        // Tag the WebView so the web UI can detect the native shell and hide
        // the browser-only chrome (fake traffic lights, outer window padding).
        configuration.applicationNameForUserAgent = "ProxyBridgeShell/1.0"
        self.webView = WKWebView(frame: .zero, configuration: configuration)

        let visibleFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1360, height: 800)
        let preferredWidth = min(CGFloat(1520), max(CGFloat(1040), visibleFrame.width - 96))
        let preferredHeight = min(CGFloat(820), max(CGFloat(640), visibleFrame.height - 96))
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: preferredWidth, height: preferredHeight),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Proxy Bridge 控制台"
        window.minSize = NSSize(width: 1040, height: 640)
        window.center()
        super.init(window: window)
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]
        webView.frame = window.contentView?.bounds ?? .zero
        window.contentView = webView
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func showAndReloadIfNeeded() {
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if webView.url == nil {
            webView.load(URLRequest(url: backendURL))
        } else {
            webView.reload()
        }
    }

    func reload() {
        if webView.url == nil {
            webView.load(URLRequest(url: backendURL))
        } else {
            webView.reload()
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var backendProcess: Process?
    private var backendPort = 18787
    private var api: BridgeAPI!
    private var latestStatus: StatusPayload?
    private var refreshTimer: Timer?
    private var lastError: String?
    private var statusMenu = NSMenu()
    private var backendLogHandle: FileHandle?
    private var consoleWindowController: ConsoleWindowController?

    private var appSupportURL: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return base.appendingPathComponent("ProxyBridge", isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installMainMenu()
        configureStatusItem()
        migrateWorkspaceStateIfNeeded()
        startBackend()
        refreshTimer = Timer.scheduledTimer(timeInterval: 4, target: self, selector: #selector(refreshNow), userInfo: nil, repeats: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.refreshNow()
        }
    }

    // 安装标准主菜单。菜单栏 App(.accessory)默认没有主菜单,导致控制台窗口里
    // 的输入框收不到 Cmd+C/V/X/A —— 这些快捷键依赖「编辑」菜单项携带的
    // key equivalent 才能沿响应链派发到 WKWebView。这里补上一个 App 菜单
    // (提供 Cmd+Q 退出)和一个标准「编辑」菜单,让复制/粘贴等快捷键恢复正常。
    private func installMainMenu() {
        let mainMenu = NSMenu()

        // App 菜单:仅保留退出项,满足 Cmd+Q 习惯。
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 Proxy Bridge", action: #selector(quitApp), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        // 编辑菜单:标准的撤销/重做/剪切/复制/粘贴/全选,均带系统快捷键。
        // action 设为 nil 让其沿响应链自动寻找第一响应者(即当前聚焦的文本框)。
        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
        refreshTimer = nil
        if let process = backendProcess, process.isRunning {
            let semaphore = DispatchSemaphore(value: 0)
            process.terminationHandler = { _ in semaphore.signal() }
            process.terminate()
            if semaphore.wait(timeout: .now() + 4) == .timedOut, process.isRunning {
                kill(process.processIdentifier, SIGKILL)
                process.waitUntilExit()
            }
        }
        backendProcess = nil
        try? backendLogHandle?.close()
        backendLogHandle = nil
    }

    private func configureStatusItem() {
        if let button = statusItem.button {
            button.title = "PB"
            button.toolTip = "Proxy Bridge"
            button.target = self
            button.action = #selector(statusItemClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        rebuildMenu()
    }

    @objc private func statusItemClicked(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            statusItem.popUpMenu(statusMenu)
        } else {
            showConsole()
        }
    }

    @objc private func showConsole() {
        if consoleWindowController == nil {
            consoleWindowController = ConsoleWindowController(backendURL: URL(string: "http://127.0.0.1:\(backendPort)")!)
        }
        consoleWindowController?.showAndReloadIfNeeded()
    }

    private func resourceBridgeScript() -> URL? {
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("bridge/app.py"), FileManager.default.fileExists(atPath: bundled.path) {
            return bundled
        }
        let source = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("app.py")
        return FileManager.default.fileExists(atPath: source.path) ? source : nil
    }

    private func startBackend() {
        do {
            try FileManager.default.createDirectory(at: appSupportURL, withIntermediateDirectories: true)
            guard let script = resourceBridgeScript() else {
                lastError = "找不到 app.py 后端脚本"
                rebuildMenu()
                return
            }
            api = BridgeAPI(port: backendPort)

            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", script.path]
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            env["PROXY_BRIDGE_PORT"] = "\(backendPort)"
            env["PROXY_BRIDGE_WORKDIR"] = appSupportURL.path
            env["PROXY_BRIDGE_PARENT_PID"] = "\(ProcessInfo.processInfo.processIdentifier)"
            process.environment = env

            let logURL = appSupportURL.appendingPathComponent("menubar-backend.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            let logHandle = try FileHandle(forWritingTo: logURL)
            logHandle.seekToEndOfFile()
            process.standardOutput = logHandle
            process.standardError = logHandle
            do {
                try process.run()
            } catch {
                try? logHandle.close()
                throw error
            }
            backendLogHandle = logHandle
            backendProcess = process
            lastError = nil
        } catch {
            lastError = error.localizedDescription
            rebuildMenu()
        }
    }

    private func migrateWorkspaceStateIfNeeded() {
        let destination = appSupportURL.appendingPathComponent("state.json")
        guard !FileManager.default.fileExists(atPath: destination.path) else { return }
        guard let resourceURL = Bundle.main.resourceURL else { return }
        let appURL = resourceURL.deletingLastPathComponent().deletingLastPathComponent()
        let projectRoot = appURL.deletingLastPathComponent().deletingLastPathComponent()
        let source = projectRoot.appendingPathComponent("work/state.json")
        guard FileManager.default.fileExists(atPath: source.path) else { return }
        do {
            try FileManager.default.createDirectory(at: appSupportURL, withIntermediateDirectories: true)
            try FileManager.default.copyItem(at: source, to: destination)
        } catch {
            lastError = "迁移旧状态失败：\(error.localizedDescription)"
        }
    }

    @objc private func refreshNow() {
        guard api != nil else {
            rebuildMenu()
            return
        }
        api.getStatus { [weak self] result in
            DispatchQueue.main.async {
                switch result {
                case .success(let status):
                    self?.latestStatus = status
                    self?.lastError = nil
                case .failure(let error):
                    self?.lastError = error.localizedDescription
                }
                self?.rebuildMenu()
            }
        }
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        let running = latestStatus?.bridges.filter { $0.status == "running" || $0.status == "starting" } ?? []
        let title = running.isEmpty ? "PB" : "PB \(running.count)"
        statusItem.button?.title = lastError == nil ? title : "PB !"
        statusItem.button?.toolTip = tooltipTitle(runningCount: running.count)

        let header = NSMenuItem(title: "Proxy Bridge", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        if let status = latestStatus {
            menu.addItem(disabledItem("sing-box: \(status.cores["sing-box"]??.isEmpty == false ? "ready" : "missing")"))
            menu.addItem(disabledItem("xray: \(status.cores["xray"]??.isEmpty == false ? "ready" : "missing")"))
            menu.addItem(disabledItem("节点 \(status.nodes.count) · 运行 \(running.count)"))
        } else if let lastError {
            menu.addItem(disabledItem("状态：\(lastError)"))
        } else {
            menu.addItem(disabledItem("后端启动中"))
        }

        menu.addItem(.separator())
        menu.addItem(actionItem("打开控制台", #selector(showConsole)))
        menu.addItem(actionItem("导入订阅 URL...", #selector(showImportURLDialog)))
        menu.addItem(actionItem("导入原始订阅...", #selector(showImportRawDialog)))
        menu.addItem(actionItem("刷新", #selector(refreshNow)))

        if let status = latestStatus {
            if !status.subscriptions.isEmpty {
                let subscriptionsMenu = NSMenu()
                for subscription in status.subscriptions {
                    let item = NSMenuItem(title: "\(subscription.name) · \(subscription.nodeCount ?? 0)", action: nil, keyEquivalent: "")
                    if subscription.url?.isEmpty == false {
                        item.action = #selector(refreshSubscription(_:))
                        item.target = self
                        item.representedObject = subscription.id
                    }
                    subscriptionsMenu.addItem(item)
                }
                let parent = NSMenuItem(title: "订阅", action: nil, keyEquivalent: "")
                parent.submenu = subscriptionsMenu
                menu.addItem(parent)
            }

            menu.addItem(.separator())
            menu.addItem(runningMenuItem(bridges: running))
            menu.addItem(nodesMenuItem(nodes: status.nodes, bridges: running, cores: status.cores))
        }

        menu.addItem(.separator())
        let stopAllItem = actionItem("停止全部端口", #selector(stopAll))
        stopAllItem.isEnabled = !running.isEmpty
        menu.addItem(stopAllItem)
        menu.addItem(actionItem("退出 Proxy Bridge", #selector(quitApp)))
        statusMenu = menu
        statusItem.menu = nil
    }

    private func tooltipTitle(runningCount: Int) -> String {
        if let lastError {
            return "Proxy Bridge：\(lastError)"
        }
        if runningCount > 0 {
            return "Proxy Bridge：\(runningCount) 个 SOCKS5 端口运行中"
        }
        return "Proxy Bridge：无运行端口"
    }

    private func runningMenuItem(bridges: [Bridge]) -> NSMenuItem {
        let parent = NSMenuItem(title: "运行中端口", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        if bridges.isEmpty {
            submenu.addItem(disabledItem("暂无运行端口"))
        } else {
            for bridge in bridges {
                let copy = NSMenuItem(title: "\(bridge.nodeName) · \(bridge.socksURL)", action: #selector(copyBridgeURL(_:)), keyEquivalent: "")
                copy.target = self
                copy.representedObject = bridge.socksURL
                submenu.addItem(copy)

                let stop = NSMenuItem(title: "停止 \(bridge.nodeName)", action: #selector(stopBridge(_:)), keyEquivalent: "")
                stop.target = self
                stop.representedObject = bridge.id
                submenu.addItem(stop)
                submenu.addItem(.separator())
            }
            if submenu.items.last?.isSeparatorItem == true {
                submenu.removeItem(at: submenu.items.count - 1)
            }
        }
        parent.submenu = submenu
        return parent
    }

    private func nodesMenuItem(nodes: [ProxyNode], bridges: [Bridge], cores: [String: String?]) -> NSMenuItem {
        let parent = NSMenuItem(title: "节点", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        if nodes.isEmpty {
            submenu.addItem(disabledItem("暂无节点，请先导入订阅"))
            parent.submenu = submenu
            return parent
        }

        let runningByNode = Dictionary(uniqueKeysWithValues: bridges.map { ($0.nodeId, $0) })
        let grouped = Dictionary(grouping: nodes) { $0.region }
        for region in grouped.keys.sorted(by: { $0.localizedStandardCompare($1) == .orderedAscending }) {
            let regionMenu = NSMenu()
            for node in (grouped[region] ?? []).sorted(by: { $0.name.localizedStandardCompare($1.name) == .orderedAscending }) {
                let itemTitle = "\(node.name) [\(node.protocolLabel)]"
                let item = NSMenuItem(title: itemTitle, action: nil, keyEquivalent: "")
                if let running = runningByNode[node.id] {
                    item.title = "\(itemTitle) · 复制 \(running.port)"
                    item.action = #selector(copyBridgeURL(_:))
                    item.target = self
                    item.representedObject = running.socksURL
                    item.state = .on
                } else {
                    item.action = #selector(startNode(_:))
                    item.target = self
                    item.representedObject = node.id
                    let core = node.preferredCore ?? "sing-box"
                    item.isEnabled = cores[core]??.isEmpty == false
                }
                regionMenu.addItem(item)
            }
            let regionItem = NSMenuItem(title: "\(region) · \(grouped[region]?.count ?? 0)", action: nil, keyEquivalent: "")
            regionItem.submenu = regionMenu
            submenu.addItem(regionItem)
        }
        parent.submenu = submenu
        return parent
    }

    private func disabledItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func actionItem(_ title: String, _ selector: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = self
        return item
    }

    @objc private func showImportURLDialog() {
        let alert = NSAlert()
        alert.messageText = "导入订阅 URL"
        alert.informativeText = "名称可留空；导入后节点会出现在菜单栏的节点列表里。"
        alert.addButton(withTitle: "导入")
        alert.addButton(withTitle: "取消")
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 8
        stack.frame = NSRect(x: 0, y: 0, width: 420, height: 88)
        let nameField = NSTextField(string: "")
        nameField.placeholderString = "名称，例如 我的机场"
        let urlField = NSTextField(string: "")
        urlField.placeholderString = "https://example.com/sub"
        stack.addArrangedSubview(nameField)
        stack.addArrangedSubview(urlField)
        alert.accessoryView = stack
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            importSubscription(name: nameField.stringValue, url: urlField.stringValue, raw: "")
        }
    }

    @objc private func showImportRawDialog() {
        let alert = NSAlert()
        alert.messageText = "导入原始订阅"
        alert.informativeText = "可以粘贴分享链接、Base64 订阅内容或 Clash/Mihomo YAML。"
        alert.addButton(withTitle: "导入")
        alert.addButton(withTitle: "取消")

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 8
        stack.frame = NSRect(x: 0, y: 0, width: 520, height: 260)
        let nameField = NSTextField(string: "")
        nameField.placeholderString = "名称，例如 手动导入"
        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 210))
        scrollView.hasVerticalScroller = true
        let textView = NSTextView(frame: scrollView.bounds)
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        scrollView.documentView = textView
        stack.addArrangedSubview(nameField)
        stack.addArrangedSubview(scrollView)
        alert.accessoryView = stack
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            importSubscription(name: nameField.stringValue, url: "", raw: textView.string)
        }
    }

    private func importSubscription(name: String, url: String, raw: String) {
        api.importSubscription(name: name, url: url, raw: raw) { [weak self] result in
            DispatchQueue.main.async {
                switch result {
                case .success(let status):
                    self?.latestStatus = status
                    self?.lastError = nil
                    self?.showNotification("订阅已导入", detail: "当前共有 \(status.nodes.count) 个节点")
                case .failure(let error):
                    self?.showError(error.localizedDescription)
                }
                self?.rebuildMenu()
            }
        }
    }

    @objc private func refreshSubscription(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        api.refreshSubscription(id: id) { [weak self] result in
            DispatchQueue.main.async {
                switch result {
                case .success(let status):
                    self?.latestStatus = status
                    self?.showNotification("订阅已刷新", detail: "当前共有 \(status.nodes.count) 个节点")
                case .failure(let error):
                    self?.showError(error.localizedDescription)
                }
                self?.rebuildMenu()
            }
        }
    }

    @objc private func startNode(_ sender: NSMenuItem) {
        guard let nodeId = sender.representedObject as? String else { return }
        sender.isEnabled = false
        api.startBridge(nodeId: nodeId) { [weak self] result in
            DispatchQueue.main.async {
                sender.isEnabled = true
                switch result {
                case .success(let bridge):
                    self?.copyToPasteboard(bridge.socksURL)
                    self?.showNotification("SOCKS5 已复制", detail: bridge.socksURL)
                    self?.refreshNow()
                case .failure(let error):
                    self?.showError(error.localizedDescription)
                }
            }
        }
    }

    @objc private func stopBridge(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        api.stopBridge(id: id) { [weak self] result in
            DispatchQueue.main.async {
                if case .failure(let error) = result {
                    self?.showError(error.localizedDescription)
                }
                self?.refreshNow()
            }
        }
    }

    @objc private func stopAll() {
        api.stopAll { [weak self] result in
            DispatchQueue.main.async {
                if case .failure(let error) = result {
                    self?.showError(error.localizedDescription)
                }
                self?.refreshNow()
            }
        }
    }

    @objc private func copyBridgeURL(_ sender: NSMenuItem) {
        guard let value = sender.representedObject as? String else { return }
        copyToPasteboard(value)
        showNotification("SOCKS5 已复制", detail: value)
    }

    private func copyToPasteboard(_ value: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(value, forType: .string)
    }

    private func showError(_ message: String) {
        lastError = message
        let alert = NSAlert()
        alert.messageText = "Proxy Bridge"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        rebuildMenu()
    }

    private func showNotification(_ title: String, detail: String) {
        let notification = NSUserNotification()
        notification.title = title
        notification.informativeText = detail
        NSUserNotificationCenter.default.deliver(notification)
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }
}

private extension ProxyNode {
    var protocolLabel: String {
        let transportPart = (transport?.isEmpty == false) ? "/\(transport!)" : ""
        let securityPart = (security?.isEmpty == false && security != transport) ? "/\(security!)" : ""
        return "\(`protocol`)\(transportPart)\(securityPart)"
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
