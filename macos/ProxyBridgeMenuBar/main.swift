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

struct GitHubRelease: Decodable {
    let tagName: String
    let assets: [GitHubAsset]

    enum CodingKeys: String, CodingKey {
        case tagName = "tag_name"
        case assets
    }
}

struct GitHubAsset: Decodable {
    let name: String
    let browserDownloadURL: String

    enum CodingKeys: String, CodingKey {
        case name
        case browserDownloadURL = "browser_download_url"
    }
}

struct CommandResult {
    let status: Int32
    let output: String
}

private final class BoundedProcessOutput {
    private let lock = NSLock()
    private let limit: Int
    private var data = Data()
    private var truncated = false

    init(limit: Int) {
        self.limit = limit
    }

    func append(_ chunk: Data) {
        lock.lock()
        defer { lock.unlock() }
        let remaining = max(0, limit - data.count)
        if remaining > 0 {
            data.append(contentsOf: chunk.prefix(remaining))
        }
        if chunk.count > remaining {
            truncated = true
        }
    }

    func string() -> String {
        lock.lock()
        defer { lock.unlock() }
        var value = String(decoding: data, as: UTF8.self)
        if truncated {
            value += "\n…输出已截断（超过 \(limit) 字节）"
        }
        return value
    }
}

enum ProxyBridgeError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let message):
            return message
        }
    }
}

final class BridgeAPI {
    private static let instanceHeader = "X-ProxyBridge-Instance"
    let baseURL: URL

    init(port: Int) {
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    func getStatus(completion: @escaping (Result<StatusPayload, Error>) -> Void) {
        request(path: "/api/status", method: "GET", body: nil, completion: completion)
    }

    func getStatus(
        expectedInstanceToken: String,
        timeout: TimeInterval,
        completion: @escaping (Result<StatusPayload, Error>) -> Void
    ) {
        request(
            path: "/api/status",
            method: "GET",
            body: nil,
            timeout: timeout,
            expectedInstanceToken: expectedInstanceToken,
            completion: completion
        )
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

    private func request<T: Decodable>(
        path: String,
        method: String,
        body: [String: Any]?,
        timeout: TimeInterval = 25,
        expectedInstanceToken: String? = nil,
        completion: @escaping (Result<T, Error>) -> Void
    ) {
        let cleanPath = path.hasPrefix("/") ? String(path.dropFirst()) : path
        var request = URLRequest(url: baseURL.appendingPathComponent(cleanPath))
        request.httpMethod = method
        request.timeoutInterval = timeout
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
            if let expectedInstanceToken {
                guard let http = response as? HTTPURLResponse,
                      http.value(forHTTPHeaderField: Self.instanceHeader) == expectedInstanceToken else {
                    completion(.failure(NSError(
                        domain: "ProxyBridge",
                        code: -2,
                        userInfo: [NSLocalizedDescriptionKey: "后端实例校验失败"]
                    )))
                    return
                }
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

    func invalidateAndClose() {
        webView.stopLoading()
        webView.load(URLRequest(url: URL(string: "about:blank")!))
        close()
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
    private var startupStatus: String?
    private var statusMenu = NSMenu()
    private var backendLogHandle: FileHandle?
    private var consoleWindowController: ConsoleWindowController?
    private let pythonReleaseAPI = URL(string: "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest")!
    private let processLifecycleLock = NSLock()
    private var helperProcesses: [Process] = []
    private var isTerminating = false
    private let bootstrapGroup = DispatchGroup()
    private var expectedBackendTerminationProcess: Process?
    private let helperTerminationGraceTimeout: TimeInterval = 2
    private let helperShutdownTimeout: TimeInterval = 4
    private let backendTerminationTimeout: TimeInterval = 12
    private let forceKillConfirmationTimeout: TimeInterval = 1
    private let backendReadinessTimeout: TimeInterval = 6
    private let backendReadinessRequestTimeout: TimeInterval = 0.75
    private let backendReadinessPollInterval: TimeInterval = 0.2
    private let processOutputFinalizationTimeout: TimeInterval = 0.75
    private let processOutputCloseConfirmationTimeout: TimeInterval = 0.1
    private let processOutputLimit = 1_048_576

    private var backendIsReady: Bool {
        guard api != nil,
              let process = backendProcess,
              process.isRunning,
              expectedBackendTerminationProcess !== process,
              !terminationRequested() else {
            return false
        }
        return true
    }

    private var appSupportURL: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return base.appendingPathComponent("ProxyBridge", isDirectory: true)
    }

    private var pythonRuntimeURL: URL {
        appSupportURL.appendingPathComponent("python-runtime", isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installMainMenu()
        configureStatusItem()
        migrateWorkspaceStateIfNeeded()
        bootstrapBackend()
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
        processLifecycleLock.lock()
        isTerminating = true
        let helpers = helperProcesses
        processLifecycleLock.unlock()

        refreshTimer?.invalidate()
        refreshTimer = nil
        invalidateConsole()

        let helperDeadline = monotonicDeadline(after: helperShutdownTimeout)
        let helperGraceDeadline = monotonicDeadline(after: helperTerminationGraceTimeout)
        for helper in helpers where helper.isRunning {
            if !sendSignal(SIGTERM, to: helper) {
                NSLog("ProxyBridge: helper SIGTERM failed for PID %d", helper.processIdentifier)
            }
        }
        _ = waitForBootstrapCleanup(until: helperGraceDeadline)
        for helper in helpers where helper.isRunning {
            if !sendSignal(SIGKILL, to: helper) {
                NSLog("ProxyBridge: helper SIGKILL failed for PID %d", helper.processIdentifier)
            }
        }
        _ = waitForBootstrapCleanup(until: helperDeadline)
        for helper in helpers where helper.isRunning {
            if !waitForProcessExit(helper, until: helperDeadline) {
                NSLog("ProxyBridge: helper PID %d did not exit before shutdown deadline", helper.processIdentifier)
            }
        }

        if let process = backendProcess, process.isRunning {
            if !sendSignal(SIGTERM, to: process) {
                NSLog("ProxyBridge: backend SIGTERM failed for PID %d", process.processIdentifier)
            }
            let backendDeadline = monotonicDeadline(after: backendTerminationTimeout)
            if !waitForProcessExit(process, until: backendDeadline), process.isRunning {
                if !sendSignal(SIGKILL, to: process) {
                    NSLog("ProxyBridge: backend SIGKILL failed for PID %d", process.processIdentifier)
                }
                let forceDeadline = monotonicDeadline(after: forceKillConfirmationTimeout)
                if !waitForProcessExit(process, until: forceDeadline) {
                    NSLog("ProxyBridge: backend PID %d did not exit after SIGKILL", process.processIdentifier)
                }
            }
        }
        api = nil
        latestStatus = nil
        expectedBackendTerminationProcess = nil
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
        } else if backendIsReady {
            showConsole()
        } else {
            rebuildMenu()
            statusItem.popUpMenu(statusMenu)
        }
    }

    @objc private func showConsole() {
        guard backendIsReady else {
            rebuildMenu()
            statusItem.popUpMenu(statusMenu)
            return
        }
        if consoleWindowController == nil {
            consoleWindowController = ConsoleWindowController(backendURL: URL(string: "http://127.0.0.1:\(backendPort)")!)
        }
        consoleWindowController?.showAndReloadIfNeeded()
    }

    private func invalidateConsole() {
        consoleWindowController?.invalidateAndClose()
        consoleWindowController = nil
    }

    private func resourceBridgeScript() -> URL? {
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("bridge/app.py"), FileManager.default.fileExists(atPath: bundled.path) {
            return bundled
        }
        let source = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("app.py")
        return FileManager.default.fileExists(atPath: source.path) ? source : nil
    }

    private func bootstrapBackend() {
        updateStartupStatus("正在准备 Python 运行时")
        let group = bootstrapGroup
        group.enter()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            defer { group.leave() }
            guard let self else { return }
            guard !self.terminationRequested() else { return }
            do {
                let pythonExecutable = try self.resolvePythonExecutable()
                DispatchQueue.main.async { [weak self] in
                    guard let self, !self.terminationRequested() else { return }
                    self.startBackend(pythonExecutable: pythonExecutable)
                }
            } catch {
                DispatchQueue.main.async { [weak self] in
                    guard let self, !self.terminationRequested() else { return }
                    self.startupStatus = nil
                    self.lastError = "Python 运行时准备失败：\(error.localizedDescription)"
                    self.rebuildMenu()
                }
            }
        }
    }

    private func terminationRequested() -> Bool {
        processLifecycleLock.lock()
        defer { processLifecycleLock.unlock() }
        return isTerminating
    }

    private func ensureNotTerminating() throws {
        if terminationRequested() {
            throw ProxyBridgeError.message("应用正在退出")
        }
    }

    private func monotonicDeadline(after interval: TimeInterval) -> UInt64 {
        let now = DispatchTime.now().uptimeNanoseconds
        let nanoseconds = UInt64(max(0, interval) * 1_000_000_000)
        if UInt64.max - now < nanoseconds {
            return UInt64.max
        }
        return now + nanoseconds
    }

    private func remainingTime(until deadline: UInt64) -> TimeInterval {
        let now = DispatchTime.now().uptimeNanoseconds
        guard deadline > now else { return 0 }
        return TimeInterval(deadline - now) / 1_000_000_000
    }

    private func waitForProcessExit(_ process: Process, until deadline: UInt64) -> Bool {
        while process.isRunning {
            let remaining = remainingTime(until: deadline)
            guard remaining > 0 else { return false }
            Thread.sleep(forTimeInterval: min(0.02, remaining))
        }
        return true
    }

    @discardableResult
    private func sendSignal(_ signal: Int32, to process: Process) -> Bool {
        guard process.isRunning else { return true }
        if kill(process.processIdentifier, signal) == 0 {
            return true
        }
        return errno == ESRCH
    }

    private func waitForBootstrapCleanup(until deadline: UInt64) -> Bool {
        let remaining = remainingTime(until: deadline)
        return bootstrapGroup.wait(timeout: .now() + remaining) == .success
    }

    private func deregisterHelper(_ process: Process) {
        processLifecycleLock.lock()
        helperProcesses.removeAll { $0 === process }
        processLifecycleLock.unlock()
    }

    private func startProcessOutputDrain(
        pipe: Pipe,
        output: BoundedProcessOutput
    ) -> DispatchSemaphore {
        let finished = DispatchSemaphore(value: 0)
        let readHandle = pipe.fileHandleForReading
        DispatchQueue.global(qos: .utility).async {
            defer { finished.signal() }
            while true {
                do {
                    guard let chunk = try readHandle.read(upToCount: 64 * 1024),
                          !chunk.isEmpty else {
                        return
                    }
                    output.append(chunk)
                } catch {
                    return
                }
            }
        }
        return finished
    }

    private func finishProcessOutputDrain(
        pipe: Pipe,
        output: BoundedProcessOutput,
        finished: DispatchSemaphore
    ) -> String {
        let deadline = monotonicDeadline(after: processOutputFinalizationTimeout)
        let timedOut = finished.wait(timeout: .now() + remainingTime(until: deadline)) == .timedOut
        try? pipe.fileHandleForReading.close()
        if timedOut {
            _ = finished.wait(timeout: .now() + processOutputCloseConfirmationTimeout)
        }
        return output.string()
    }

    private func closeProcessOutputDrain(pipe: Pipe, finished: DispatchSemaphore) {
        try? pipe.fileHandleForReading.close()
        _ = finished.wait(timeout: .now() + processOutputCloseConfirmationTimeout)
    }

    private func updateStartupStatus(_ message: String?) {
        guard !terminationRequested() else { return }
        let apply = { [weak self] in
            guard let self, !self.terminationRequested() else { return }
            self.startupStatus = message
            if message != nil {
                self.lastError = nil
            }
            self.rebuildMenu()
        }
        if Thread.isMainThread {
            apply()
        } else {
            DispatchQueue.main.async(execute: apply)
        }
    }

    private func resolvePythonExecutable() throws -> URL {
        try FileManager.default.createDirectory(at: appSupportURL, withIntermediateDirectories: true)
        if let bundled = bundledPythonExecutable() {
            updateStartupStatus("使用内置 Python 运行时")
            return bundled
        }
        if let privateRuntime = privatePythonExecutable() {
            updateStartupStatus("使用已安装的 Python 运行时")
            return privateRuntime
        }
        if let systemRuntime = systemPythonExecutable() {
            updateStartupStatus("使用本机 Python 运行时")
            return systemRuntime
        }
        return try installPythonRuntime()
    }

    private func bundledPythonExecutable() -> URL? {
        guard let resourceURL = Bundle.main.resourceURL else { return nil }
        let candidates = [
            resourceURL.appendingPathComponent("python-runtime/python/bin/python3"),
            resourceURL.appendingPathComponent("bridge/python-runtime/python/bin/python3")
        ]
        return firstUsablePythonExecutable(in: candidates)
    }

    private func privatePythonExecutable() -> URL? {
        let direct = pythonRuntimeURL.appendingPathComponent("python/bin/python3")
        if isUsablePythonExecutable(direct) {
            return direct
        }
        return findPythonExecutable(in: pythonRuntimeURL).flatMap { isUsablePythonExecutable($0) ? $0 : nil }
    }

    private func systemPythonExecutable() -> URL? {
        let candidates = [
            URL(fileURLWithPath: "/opt/homebrew/bin/python3"),
            URL(fileURLWithPath: "/usr/local/bin/python3"),
            URL(fileURLWithPath: "/Library/Frameworks/Python.framework/Versions/Current/bin/python3")
        ]
        return firstUsablePythonExecutable(in: candidates)
    }

    private func firstUsablePythonExecutable(in candidates: [URL]) -> URL? {
        for candidate in candidates where isUsablePythonExecutable(candidate) {
            return candidate
        }
        return nil
    }

    private func isUsablePythonExecutable(_ url: URL) -> Bool {
        guard FileManager.default.isExecutableFile(atPath: url.path) else { return false }
        let script = "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        do {
            let result = try runProcess(url.path, ["-c", script], timeout: 15)
            return result.status == 0
        } catch {
            return false
        }
    }

    private func installPythonRuntime() throws -> URL {
        try ensureNotTerminating()
        updateStartupStatus("正在查询 Python 运行时版本")
        let asset = try latestPythonRuntimeAsset()
        try ensureNotTerminating()
        updateStartupStatus("正在下载 Python 运行时")

        let fileManager = FileManager.default
        removeStalePythonRuntimeInstallations(fileManager: fileManager)
        let tempRoot = appSupportURL.appendingPathComponent("python-runtime-install-\(UUID().uuidString)", isDirectory: true)
        let extractRoot = tempRoot.appendingPathComponent("extract", isDirectory: true)
        let stagedRuntime = appSupportURL.appendingPathComponent("python-runtime-staged-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? fileManager.removeItem(at: tempRoot)
            try? fileManager.removeItem(at: stagedRuntime)
        }

        try fileManager.createDirectory(at: extractRoot, withIntermediateDirectories: true)
        let archiveURL = tempRoot.appendingPathComponent(asset.name)
        try downloadPythonArchive(asset: asset, to: archiveURL)
        try ensureNotTerminating()

        updateStartupStatus("正在安装 Python 运行时")
        let extractResult = try runProcess("/usr/bin/tar", ["-xzf", archiveURL.path, "-C", extractRoot.path], timeout: 180)
        guard extractResult.status == 0 else {
            throw ProxyBridgeError.message("解压 Python 运行时失败：\(trimmedOutput(extractResult.output))")
        }

        guard let extractedPython = findPythonExecutable(in: extractRoot) else {
            throw ProxyBridgeError.message("下载的 Python 运行时中没有找到 python3")
        }
        try ensureNotTerminating()

        let pythonHome = extractedPython.deletingLastPathComponent().deletingLastPathComponent()
        try fileManager.createDirectory(at: stagedRuntime, withIntermediateDirectories: true)
        try fileManager.moveItem(at: pythonHome, to: stagedRuntime.appendingPathComponent("python", isDirectory: true))

        try ensureNotTerminating()
        if fileManager.fileExists(atPath: pythonRuntimeURL.path) {
            try fileManager.removeItem(at: pythonRuntimeURL)
        }
        try fileManager.moveItem(at: stagedRuntime, to: pythonRuntimeURL)

        let installedPython = pythonRuntimeURL.appendingPathComponent("python/bin/python3")
        if !fileManager.isExecutableFile(atPath: installedPython.path) {
            try fileManager.setAttributes([.posixPermissions: NSNumber(value: 0o755)], ofItemAtPath: installedPython.path)
        }
        guard isUsablePythonExecutable(installedPython) else {
            throw ProxyBridgeError.message("安装后的 Python 运行时无法启动")
        }
        updateStartupStatus("Python 运行时已安装")
        return installedPython
    }

    private func removeStalePythonRuntimeInstallations(fileManager: FileManager) {
        guard let children = try? fileManager.contentsOfDirectory(
            at: appSupportURL,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else {
            return
        }
        for child in children {
            let name = child.lastPathComponent
            guard name.hasPrefix("python-runtime-install-") ||
                  name.hasPrefix("python-runtime-staged-") else {
                continue
            }
            try? fileManager.removeItem(at: child)
        }
    }

    private func latestPythonRuntimeAsset() throws -> GitHubAsset {
        var request = URLRequest(url: pythonReleaseAPI)
        request.timeoutInterval = 30
        request.addValue("ProxyBridge/0.1", forHTTPHeaderField: "User-Agent")
        request.addValue("application/vnd.github+json", forHTTPHeaderField: "Accept")

        let data = try fetchData(request: request, timeout: 35)
        let release = try JSONDecoder().decode(GitHubRelease.self, from: data)
        let arch = hostPythonAssetArch()
        let ranked = release.assets
            .map { (pythonAssetScore(name: $0.name, arch: arch), $0) }
            .filter { $0.0 >= 0 }
            .sorted { $0.0 > $1.0 }
        guard let asset = ranked.first?.1 else {
            throw ProxyBridgeError.message("未找到适配 macOS/\(arch) 的 Python 运行时资源")
        }
        return asset
    }

    private func hostPythonAssetArch() -> String {
        if let result = try? runProcess("/usr/bin/uname", ["-m"], timeout: 5) {
            let machine = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            if machine == "arm64" {
                return "aarch64"
            }
            if machine == "x86_64" {
                return "x86_64"
            }
        }
        #if arch(arm64)
        return "aarch64"
        #else
        return "x86_64"
        #endif
    }

    private func pythonAssetScore(name: String, arch: String) -> Int {
        let lower = name.lowercased()
        guard lower.hasSuffix(".tar.gz"),
              lower.contains("cpython-"),
              lower.contains("\(arch)-apple-darwin"),
              lower.contains("install_only"),
              !lower.contains("debug"),
              !lower.contains("freethreaded") else {
            return -1
        }
        guard let version = parsePythonVersion(from: lower),
              version.major == 3,
              version.minor >= 10,
              version.isStable else {
            return -1
        }

        var score = version.minor * 10_000 + version.patch * 100
        if lower.contains("install_only_stripped") {
            score += 1_000
        }
        return score
    }

    private func parsePythonVersion(from name: String) -> (major: Int, minor: Int, patch: Int, isStable: Bool)? {
        guard let prefixRange = name.range(of: "cpython-") else { return nil }
        let remainder = name[prefixRange.upperBound...]
        let versionText = remainder.split(whereSeparator: { $0 == "+" || $0 == "-" }).first ?? Substring(remainder)
        let parts = versionText.split(separator: ".")
        guard parts.count >= 2,
              let major = Int(parts[0]),
              let minor = Int(parts[1]) else {
            return nil
        }
        let patchText = parts.count >= 3 ? String(parts[2]) : "0"
        let patchDigits = patchText.prefix { $0.isNumber }
        guard !patchDigits.isEmpty, let patch = Int(patchDigits) else { return nil }
        let isStable = patchDigits.count == patchText.count
        return (major, minor, patch, isStable)
    }

    private func downloadPythonArchive(asset: GitHubAsset, to archiveURL: URL) throws {
        guard let downloadURL = URL(string: asset.browserDownloadURL) else {
            throw ProxyBridgeError.message("Python 运行时下载地址无效")
        }
        let result = try runProcess(
            "/usr/bin/curl",
            [
                "--fail",
                "--location",
                "--retry", "2",
                "--connect-timeout", "20",
                "--user-agent", "ProxyBridge/0.1",
                "--silent",
                "--show-error",
                "--output", archiveURL.path,
                downloadURL.absoluteString
            ],
            timeout: 900
        )
        guard result.status == 0 else {
            throw ProxyBridgeError.message("下载 Python 运行时失败：\(trimmedOutput(result.output))")
        }
    }

    private func findPythonExecutable(in root: URL) -> URL? {
        let fileManager = FileManager.default
        let direct = root.appendingPathComponent("python/bin/python3")
        if fileManager.isExecutableFile(atPath: direct.path) {
            return direct
        }
        guard fileManager.fileExists(atPath: root.path),
              let enumerator = fileManager.enumerator(at: root, includingPropertiesForKeys: nil) else {
            return nil
        }
        for case let url as URL in enumerator {
            if url.lastPathComponent == "python3",
               url.deletingLastPathComponent().lastPathComponent == "bin",
               fileManager.isExecutableFile(atPath: url.path) {
                return url
            }
        }
        return nil
    }

    private func fetchData(request: URLRequest, timeout: TimeInterval) throws -> Data {
        let semaphore = DispatchSemaphore(value: 0)
        var result: Result<(Data, URLResponse), Error>?
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                result = .failure(error)
            } else if let data, let response {
                result = .success((data, response))
            } else {
                result = .failure(ProxyBridgeError.message("网络请求没有返回数据"))
            }
            semaphore.signal()
        }
        task.resume()
        if semaphore.wait(timeout: .now() + timeout) == .timedOut {
            task.cancel()
            throw ProxyBridgeError.message("请求 GitHub 超时")
        }
        switch result {
        case .success(let (data, response)):
            if let httpResponse = response as? HTTPURLResponse,
               !(200...299).contains(httpResponse.statusCode) {
                throw ProxyBridgeError.message("GitHub 返回 HTTP \(httpResponse.statusCode)")
            }
            return data
        case .failure(let error):
            throw error
        case .none:
            throw ProxyBridgeError.message("网络请求失败")
        }
    }

    private func runProcess(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws -> CommandResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let output = BoundedProcessOutput(limit: processOutputLimit)

        let exitSemaphore = DispatchSemaphore(value: 0)
        process.terminationHandler = { [weak self] terminatedProcess in
            exitSemaphore.signal()
            self?.deregisterHelper(terminatedProcess)
        }
        processLifecycleLock.lock()
        guard !isTerminating else {
            processLifecycleLock.unlock()
            try? pipe.fileHandleForReading.close()
            try? pipe.fileHandleForWriting.close()
            throw ProxyBridgeError.message("应用正在退出")
        }
        helperProcesses.append(process)
        do {
            try process.run()
        } catch {
            helperProcesses.removeAll { $0 === process }
            processLifecycleLock.unlock()
            try? pipe.fileHandleForReading.close()
            try? pipe.fileHandleForWriting.close()
            throw error
        }
        processLifecycleLock.unlock()

        let drainFinished = startProcessOutputDrain(pipe: pipe, output: output)
        try? pipe.fileHandleForWriting.close()
        let executionDeadline = monotonicDeadline(after: timeout)
        if exitSemaphore.wait(timeout: .now() + remainingTime(until: executionDeadline)) == .timedOut {
            if !sendSignal(SIGTERM, to: process) {
                NSLog("ProxyBridge: helper SIGTERM failed for PID %d", process.processIdentifier)
            }
            let terminateDeadline = monotonicDeadline(after: forceKillConfirmationTimeout)
            if !waitForProcessExit(process, until: terminateDeadline), process.isRunning {
                guard sendSignal(SIGKILL, to: process) else {
                    closeProcessOutputDrain(pipe: pipe, finished: drainFinished)
                    throw ProxyBridgeError.message(
                        "\(URL(fileURLWithPath: executable).lastPathComponent) 执行超时且无法发送 SIGKILL"
                    )
                }
                let forceDeadline = monotonicDeadline(after: forceKillConfirmationTimeout)
                guard waitForProcessExit(process, until: forceDeadline) else {
                    closeProcessOutputDrain(pipe: pipe, finished: drainFinished)
                    throw ProxyBridgeError.message(
                        "\(URL(fileURLWithPath: executable).lastPathComponent) 执行超时且无法确认退出"
                    )
                }
            }
            _ = finishProcessOutputDrain(pipe: pipe, output: output, finished: drainFinished)
            throw ProxyBridgeError.message("\(URL(fileURLWithPath: executable).lastPathComponent) 执行超时")
        }

        let exitConfirmationDeadline = monotonicDeadline(after: forceKillConfirmationTimeout)
        guard waitForProcessExit(process, until: exitConfirmationDeadline) else {
            closeProcessOutputDrain(pipe: pipe, finished: drainFinished)
            throw ProxyBridgeError.message(
                "\(URL(fileURLWithPath: executable).lastPathComponent) 已通知退出但无法确认"
            )
        }
        let capturedOutput = finishProcessOutputDrain(pipe: pipe, output: output, finished: drainFinished)
        return CommandResult(status: process.terminationStatus, output: capturedOutput)
    }

    private func trimmedOutput(_ output: String) -> String {
        let value = output.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty {
            return "无详细输出"
        }
        return value
    }

    private func startBackend(pythonExecutable: URL) {
        guard !terminationRequested() else { return }
        invalidateConsole()
        api = nil
        latestStatus = nil
        startupStatus = "正在启动后端"
        lastError = nil
        rebuildMenu()

        do {
            try FileManager.default.createDirectory(at: appSupportURL, withIntermediateDirectories: true)
            guard let script = resourceBridgeScript() else {
                lastError = "找不到 app.py 后端脚本"
                startupStatus = nil
                api = nil
                latestStatus = nil
                rebuildMenu()
                return
            }
            let candidateAPI = BridgeAPI(port: backendPort)
            let instanceToken = UUID().uuidString

            let process = Process()
            process.executableURL = pythonExecutable
            process.arguments = [script.path]
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "\(pythonExecutable.deletingLastPathComponent().path):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            env.removeValue(forKey: "PYTHONHOME")
            env.removeValue(forKey: "PYTHONPATH")
            env["PROXY_BRIDGE_PORT"] = "\(backendPort)"
            env["PROXY_BRIDGE_WORKDIR"] = appSupportURL.path
            env["PROXY_BRIDGE_PARENT_PID"] = "\(ProcessInfo.processInfo.processIdentifier)"
            env["PROXY_BRIDGE_INSTANCE_TOKEN"] = instanceToken
            process.environment = env

            let logURL = appSupportURL.appendingPathComponent("menubar-backend.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            let logHandle = try FileHandle(forWritingTo: logURL)
            logHandle.seekToEndOfFile()
            process.standardOutput = logHandle
            process.standardError = logHandle
            process.terminationHandler = { [weak self] terminatedProcess in
                self?.backendDidTerminate(terminatedProcess, logHandle: logHandle)
            }
            do {
                try process.run()
            } catch {
                try? logHandle.close()
                throw error
            }
            backendLogHandle = logHandle
            backendProcess = process
            let readinessDeadline = monotonicDeadline(after: backendReadinessTimeout)
            DispatchQueue.main.asyncAfter(deadline: .now() + backendReadinessTimeout) { [weak self] in
                guard let self,
                      self.backendProcess === process,
                      self.api == nil,
                      self.expectedBackendTerminationProcess !== process else {
                    return
                }
                self.failBackendStartup(
                    process: process,
                    message: "后端启动失败：/api/status 在 6 秒内未就绪"
                )
            }
            waitForBackendReady(
                candidateAPI: candidateAPI,
                process: process,
                instanceToken: instanceToken,
                deadline: readinessDeadline
            )
        } catch {
            startupStatus = nil
            api = nil
            latestStatus = nil
            backendProcess = nil
            try? backendLogHandle?.close()
            backendLogHandle = nil
            guard !terminationRequested() else { return }
            lastError = "后端启动失败：\(error.localizedDescription)"
            rebuildMenu()
        }
    }

    private func waitForBackendReady(
        candidateAPI: BridgeAPI,
        process: Process,
        instanceToken: String,
        deadline: UInt64
    ) {
        guard backendProcess === process,
              expectedBackendTerminationProcess !== process else {
            return
        }
        guard process.isRunning else {
            failBackendStartup(
                process: process,
                message: "后端进程在就绪前退出（状态码 \(process.terminationStatus)）"
            )
            return
        }
        let remaining = remainingTime(until: deadline)
        guard remaining > 0 else {
            failBackendStartup(process: process, message: "后端启动失败：/api/status 在 6 秒内未就绪")
            return
        }

        candidateAPI.getStatus(
            expectedInstanceToken: instanceToken,
            timeout: min(backendReadinessRequestTimeout, remaining)
        ) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.backendProcess === process,
                      self.expectedBackendTerminationProcess !== process else {
                    return
                }
                guard process.isRunning else {
                    self.failBackendStartup(
                        process: process,
                        message: "后端进程在就绪前退出（状态码 \(process.terminationStatus)）"
                    )
                    return
                }
                guard self.remainingTime(until: deadline) > 0 else {
                    self.failBackendStartup(
                        process: process,
                        message: "后端启动失败：/api/status 在 6 秒内未就绪"
                    )
                    return
                }

                switch result {
                case .success(let status):
                    self.api = candidateAPI
                    self.latestStatus = status
                    self.startupStatus = nil
                    self.lastError = nil
                    self.rebuildMenu()
                case .failure:
                    DispatchQueue.main.asyncAfter(deadline: .now() + self.backendReadinessPollInterval) { [weak self] in
                        self?.waitForBackendReady(
                            candidateAPI: candidateAPI,
                            process: process,
                            instanceToken: instanceToken,
                            deadline: deadline
                        )
                    }
                }
            }
        }
    }

    private func backendDidTerminate(_ process: Process, logHandle: FileHandle) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let expectedStartupFailure = self.expectedBackendTerminationProcess === process
            let exitedBeforeReadiness = self.api == nil
            guard self.clearBackendState(for: process, matching: logHandle) else {
                return
            }
            guard !self.terminationRequested() else { return }
            self.startupStatus = nil
            if expectedStartupFailure {
                self.rebuildMenu()
                return
            }
            if exitedBeforeReadiness {
                self.lastError = "后端进程在就绪前退出（状态码 \(process.terminationStatus)）"
            } else {
                self.lastError = "后端进程意外退出（状态码 \(process.terminationStatus)）"
            }
            self.rebuildMenu()
        }
    }

    private func failBackendStartup(process: Process, message: String) {
        guard backendProcess === process else { return }
        invalidateConsole()
        api = nil
        latestStatus = nil
        expectedBackendTerminationProcess = process
        if !terminationRequested() {
            startupStatus = nil
            lastError = message
            rebuildMenu()
        }
        if process.isRunning {
            if !sendSignal(SIGTERM, to: process) {
                if !terminationRequested() {
                    lastError = "\(message)；无法发送 SIGTERM"
                    rebuildMenu()
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + backendTerminationTimeout) { [weak self] in
                guard let self,
                      self.backendProcess === process,
                      self.expectedBackendTerminationProcess === process,
                      process.isRunning else {
                    return
                }
                guard self.sendSignal(SIGKILL, to: process) else {
                    if !self.terminationRequested() {
                        self.lastError = "\(message)；无法发送 SIGKILL"
                        self.rebuildMenu()
                    }
                    return
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + self.forceKillConfirmationTimeout) { [weak self] in
                    guard let self,
                          self.backendProcess === process,
                          self.expectedBackendTerminationProcess === process,
                          process.isRunning,
                          !self.terminationRequested() else {
                        return
                    }
                    self.lastError = "\(message)；无法确认后端退出"
                    self.rebuildMenu()
                }
            }
        }
    }

    @discardableResult
    private func clearBackendState(for process: Process, matching logHandle: FileHandle?) -> Bool {
        guard backendProcess === process else { return false }
        invalidateConsole()
        backendProcess = nil
        api = nil
        latestStatus = nil
        if expectedBackendTerminationProcess === process {
            expectedBackendTerminationProcess = nil
        }
        if let logHandle, backendLogHandle === logHandle {
            backendLogHandle = nil
            try? logHandle.close()
        }
        return true
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
        guard let currentAPI = api,
              let process = backendProcess,
              process.isRunning else {
            rebuildMenu()
            return
        }
        currentAPI.getStatus { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.api === currentAPI,
                      self.backendProcess === process,
                      process.isRunning else {
                    return
                }
                switch result {
                case .success(let status):
                    self.latestStatus = status
                    self.lastError = nil
                case .failure(let error):
                    self.lastError = error.localizedDescription
                }
                self.rebuildMenu()
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
        } else if let startupStatus {
            menu.addItem(disabledItem("状态：\(startupStatus)"))
        } else if let lastError {
            menu.addItem(disabledItem("状态：\(lastError)"))
        } else {
            menu.addItem(disabledItem("后端启动中"))
        }

        menu.addItem(.separator())
        let backendReady = backendIsReady
        let consoleItem = actionItem("打开控制台", #selector(showConsole))
        consoleItem.isEnabled = backendReady
        menu.addItem(consoleItem)
        let importURLItem = actionItem("导入订阅 URL...", #selector(showImportURLDialog))
        importURLItem.isEnabled = backendReady
        menu.addItem(importURLItem)
        let importRawItem = actionItem("导入原始订阅...", #selector(showImportRawDialog))
        importRawItem.isEnabled = backendReady
        menu.addItem(importRawItem)
        let refreshItem = actionItem("刷新", #selector(refreshNow))
        refreshItem.isEnabled = backendReady
        menu.addItem(refreshItem)

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
        if let startupStatus {
            return "Proxy Bridge：\(startupStatus)"
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

        let runningByNode = bridges.reduce(into: [String: Bridge]()) { result, bridge in
            result[bridge.nodeId] = bridge
        }
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
        guard let currentAPI = api,
              let process = backendProcess,
              process.isRunning else {
            return
        }
        currentAPI.importSubscription(name: name, url: url, raw: raw) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.api === currentAPI,
                      self.backendProcess === process,
                      process.isRunning else {
                    return
                }
                switch result {
                case .success(let status):
                    self.latestStatus = status
                    self.lastError = nil
                    self.showNotification("订阅已导入", detail: "当前共有 \(status.nodes.count) 个节点")
                case .failure(let error):
                    self.showError(error.localizedDescription)
                }
                self.rebuildMenu()
            }
        }
    }

    @objc private func refreshSubscription(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let currentAPI = api,
              let process = backendProcess,
              process.isRunning else {
            return
        }
        currentAPI.refreshSubscription(id: id) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.api === currentAPI,
                      self.backendProcess === process,
                      process.isRunning else {
                    return
                }
                switch result {
                case .success(let status):
                    self.latestStatus = status
                    self.showNotification("订阅已刷新", detail: "当前共有 \(status.nodes.count) 个节点")
                case .failure(let error):
                    self.showError(error.localizedDescription)
                }
                self.rebuildMenu()
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
        guard !terminationRequested() else { return }
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
