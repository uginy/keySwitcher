//
//  KeySwitcher — menu bar switcher for Codex ChatGPT accounts.
//  Single-file AppKit + SwiftUI hybrid. UI language: Russian.
//
//  Talks to the Python engine (keyswitcher.py) which prints exactly one
//  JSON object to stdout per invocation. Tokens are never read or shown here.
//

import AppKit
import Combine
import ServiceManagement
import SwiftUI
import UserNotifications

// MARK: - Engine JSON contract (Codable models, tolerant optionals)

struct StatusResponse: Codable, Sendable {
    var ok: Bool?
    var generated_at: Int?
    var active_slot: Int?
    var active_account_id: String?
    var daemon: DaemonInfo?
    var autoswitch: AutoswitchInfo?
    var accounts: [AccountInfo]?
    var error: String?
}

struct DaemonInfo: Codable, Sendable {
    var running: Bool?
}

struct AutoswitchInfo: Codable, Sendable {
    var enabled: Bool?
    var cooldown_until: Int?
}

struct AccountInfo: Codable, Sendable {
    var slot: Int
    var file: String?
    var email: String?
    var account_id: String?
    var active: Bool?
    var plan: String?
    var usage: UsageInfo?
    var error: String?
}

struct UsageInfo: Codable, Sendable {
    var ok: Bool?
    var fetched_at: Int?
    var stale: Bool?
    var allowed: Bool?
    var primary: UsageWindow?
    var secondary: UsageWindow?
    var credits_balance: Double?
    var error: String?
}

struct UsageWindow: Codable, Sendable {
    var used_percent: Double?
    var reset_at: Int?
    var window_minutes: Int?
}

struct SwitchResponse: Codable, Sendable {
    var ok: Bool?
    var slot: Int?
    var email: String?
    var error: String?
    var log: [String]?
}

struct DaemonResponse: Codable, Sendable {
    var ok: Bool?
    var running: Bool?
    var error: String?
}

struct ConfigResponse: Codable, Sendable {
    var ok: Bool?
    var config: ConfigData?
    var error: String?
}

struct ConfigData: Codable, Sendable {
    var autoswitch_enabled: Bool?
    var notifications: Bool?
    var tray_display: String?
    var antigravity_tray_target: String?
    var antigravity_tray_models: String?
    var tray_slots: [String]?
}

enum TraySlotItem: String, CaseIterable, Identifiable, Sendable {
    case codex = "codex"
    case agCliGemini = "ag_cli_gemini"
    case agCliClaude = "ag_cli_claude"
    case agIdeGemini = "ag_ide_gemini"
    case agIdeClaude = "ag_ide_claude"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .codex: return L10n.codexSlotTitle
        case .agCliGemini: return L10n.agCliGeminiTitle
        case .agCliClaude: return L10n.agCliClaudeTitle
        case .agIdeGemini: return L10n.agIdeGeminiTitle
        case .agIdeClaude: return L10n.agIdeClaudeTitle
        }
    }

    var description: String {
        switch self {
        case .codex: return L10n.codexSlotDesc
        case .agCliGemini: return L10n.agCliGeminiDesc
        case .agCliClaude: return L10n.agCliClaudeDesc
        case .agIdeGemini: return L10n.agIdeGeminiDesc
        case .agIdeClaude: return L10n.agIdeClaudeDesc
        }
    }

    var systemIcon: String {
        switch self {
        case .codex: return "circle.hexagongrid.fill"
        case .agCliGemini: return "sparkle"
        case .agCliClaude: return "bolt.fill"
        case .agIdeGemini: return "sparkle"
        case .agIdeClaude: return "bolt.fill"
        }
    }

    var iconColor: Color {
        switch self {
        case .codex: return .green
        case .agCliGemini: return .blue
        case .agCliClaude: return .orange
        case .agIdeGemini: return .purple
        case .agIdeClaude: return .pink
        }
    }
}

enum TrayDisplay: String, CaseIterable, Sendable {
    case both
    case codex
    case antigravity

    var title: String {
        switch self {
        case .both: return L10n.trayDisplayBoth
        case .codex: return L10n.trayDisplayCodex
        case .antigravity: return L10n.trayDisplayAntigravity
        }
    }
}

enum AntigravityTrayTarget: String, CaseIterable, Sendable {
    case both
    case cli
    case ide

    var title: String {
        switch self {
        case .both: return L10n.antigravityTrayTargetBoth
        case .cli: return L10n.antigravityTrayTargetCli
        case .ide: return L10n.antigravityTrayTargetIde
        }
    }
}

enum AntigravityTrayModels: String, CaseIterable, Sendable {
    case both
    case gemini
    case claudeGpt = "claude_gpt"

    var title: String {
        switch self {
        case .both: return L10n.antigravityTrayBoth
        case .gemini: return L10n.antigravityTrayGemini
        case .claudeGpt: return L10n.antigravityTrayClaudeGpt
        }
    }
}

// MARK: - Engine client (Process + per-command watchdog, serial utility queue)

enum EngineError: Error, Sendable {
    case engineMissing
    case launchFailed(String)
    case timedOut(seconds: Int)
    case badOutput(String)

    var displayText: String {
        switch self {
        case .engineMissing: return L10n.engineMissingTitle
        case .launchFailed(let message):
            return LanguageManager.shared.isRussian ? "Не удалось запустить движок: \(message)" : "Failed to launch engine: \(message)"
        case .timedOut(let seconds):
            return LanguageManager.shared.isRussian ? "Движок не ответил за \(seconds) с" : "Engine timed out after \(seconds)s"
        case .badOutput(let message):
            return LanguageManager.shared.isRussian ? "Некорректный ответ движка: \(message)" : "Invalid engine response: \(message)"
        }
    }

    var isEngineMissing: Bool {
        if case .engineMissing = self { return true }
        return false
    }

    var isTimedOut: Bool {
        if case .timedOut = self { return true }
        return false
    }
}

final class EngineClient {
    /// Switch and autoswitch-check can legitimately run for a while (rotator.py may
    /// wait on the TCC consent dialog); status/switch must comfortably exceed the
    /// engine's serialized token-refresh worst case (~30s under degraded network),
    /// so the watchdog never SIGTERMs a refresh mid-flight and bricks a slot.
    static let switchTimeout: TimeInterval = 60

    private let queue = DispatchQueue(label: "com.eugene.keyswitcher.engine", qos: .utility)
    private let pythonPath = "/usr/bin/python3"

    /// Per-command watchdog budget — the engine's worst cases differ a lot:
    /// - status: serialized token refreshes (10s HTTP timeout each) + usage fetch
    /// - switch / autoswitch-check: rotator.py can block on the first Automation/Accessibility prompt
    /// - config: pure local file I/O
    private func watchdogTimeout(for args: [String]) -> TimeInterval {
        switch args.first {
        case "config": return 20
        case "relogin": return 330 // interactive browser OAuth: up to 5 min + slack
        case "add": return 330
        case "delete": return 20
        default: return EngineClient.switchTimeout // status, switch, autoswitch-check
        }
    }

    /// Resolution order: env KEYSWITCHER_ENGINE → bundle Resources/keyswitcher.py
    /// → dev path in the project tree (engine/keyswitcher.py).
    ///
    /// The engine is never read from ~/.codex — it ships inside the app bundle so
    /// that wiping or reinstalling Codex can't take the KeySwitcher code with it.
    /// ~/.codex only ever holds the engine's runtime data (config/cache/state).
    func resolveEnginePath() -> String? {
        let fileManager = FileManager.default
        var candidates: [String] = []
        if let envPath = ProcessInfo.processInfo.environment["KEYSWITCHER_ENGINE"], !envPath.isEmpty {
            candidates.append(envPath)
        }
        if let resourcePath = Bundle.main.resourceURL?.appendingPathComponent("keyswitcher.py").path {
            candidates.append(resourcePath)
        }
        // Last resort for running the unbundled binary in dev: the engine next
        // to this source file. #filePath is resolved at build time to the path
        // swiftc was given, so it tracks the project wherever it's rebuilt.
        candidates.append(URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // app/
            .deletingLastPathComponent()   // project root
            .appendingPathComponent("engine/keyswitcher.py").path)
        for candidate in candidates where fileManager.isReadableFile(atPath: candidate) {
            return candidate
        }
        return nil
    }

    /// Runs the engine on the serial utility queue; completion always hops to main.
    func run<T: Decodable & Sendable>(_ args: [String], as type: T.Type, completion: @escaping (Result<T, EngineError>) -> Void) {
        queue.async {
            let result = self.runSynchronously(args, as: type)
            DispatchQueue.main.async { completion(result) }
        }
    }

    private func runSynchronously<T: Decodable & Sendable>(_ args: [String], as type: T.Type) -> Result<T, EngineError> {
        guard let enginePath = resolveEnginePath() else { return .failure(.engineMissing) }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [enginePath] + args
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        do {
            try process.run()
        } catch {
            return .failure(.launchFailed(error.localizedDescription))
        }

        // Watchdog: a hung engine must never freeze the app.
        let timeout = watchdogTimeout(for: args)
        let timedOutFlag = TimeoutFlag()
        let watchdog = DispatchWorkItem {
            if process.isRunning {
                timedOutFlag.set()
                process.terminate()
            }
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: watchdog)

        // Drain stderr on a side queue so a chatty engine cannot deadlock the pipes.
        DispatchQueue.global(qos: .utility).async {
            _ = try? stderrPipe.fileHandleForReading.readToEnd()
        }

        let outputData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        watchdog.cancel()

        do {
            // Decode first: complete valid JSON means the engine finished its work,
            // even if the watchdog raced the process exit and set the flag (TOCTOU).
            let decoded = try JSONDecoder().decode(T.self, from: outputData)
            return .success(decoded)
        } catch {
            if timedOutFlag.isSet { return .failure(.timedOut(seconds: Int(timeout))) }
            // Never echo raw engine output here (defense in depth for token safety).
            let summary = String(String(describing: error).prefix(180))
            return .failure(.badOutput(summary))
        }
    }
}

/// Tiny thread-safe boolean for the watchdog.
final class TimeoutFlag {
    private let lock = NSLock()
    private var value = false
    func set() { lock.lock(); value = true; lock.unlock() }
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

// MARK: - User notifications (guarded for non-bundle runs)

enum Notifier {
    /// UNUserNotificationCenter throws ObjC exceptions without a proper bundle.
    static let isAvailable: Bool = (Bundle.main.bundleIdentifier != nil)

    static func requestAuthorization() {
        guard isAvailable else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in
            // Denial is fine; we just stay silent.
        }
    }

    static func post(body: String) {
        guard isAvailable else { return }
        let content = UNMutableNotificationContent()
        content.title = "Codex"
        content.body = body
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { _ in }
    }
}

// MARK: - Observable app state

final class AppState: ObservableObject {
    @Published var status: StatusResponse?
    @Published var config: ConfigData?
    @Published var engineMissing = false
    @Published var statusFailed = false
    @Published var lastErrorText: String?
    @Published var isLoading = false
    @Published var isSwitching = false
    @Published var switchingSlot: Int?
    @Published var reloginingSlot: Int?
    @Published var deletingSlot: Int?
    @Published var isAdding = false
    @Published var autoswitchEnabled = false   // opt-in; ON drives the reactive daemon
    @Published var daemonRunning = false        // tracked internally; ON mirrors autoswitch
    @Published var loginItemEnabled = false

    var notificationsEnabled: Bool { config?.notifications ?? true }
    var trayDisplay: TrayDisplay { TrayDisplay(rawValue: config?.tray_display ?? "") ?? .both }
    var antigravityTrayTarget: AntigravityTrayTarget {
        AntigravityTrayTarget(rawValue: config?.antigravity_tray_target ?? "") ?? .both
    }
    var antigravityTrayModels: AntigravityTrayModels {
        AntigravityTrayModels(rawValue: config?.antigravity_tray_models ?? "") ?? .both
    }
    var traySlots: [TraySlotItem] {
        if let rawSlots = config?.tray_slots {
            var result: [TraySlotItem] = []
            for raw in rawSlots {
                if raw == "ag_cli" {
                    result.append(.agCliGemini)
                    result.append(.agCliClaude)
                } else if raw == "ag_ide" {
                    result.append(.agIdeGemini)
                    result.append(.agIdeClaude)
                } else if let item = TraySlotItem(rawValue: raw) {
                    result.append(item)
                }
            }
            return result
        }
        var slots: [TraySlotItem] = []
        if trayDisplay != .antigravity {
            slots.append(.codex)
        }
        if trayDisplay != .codex {
            if antigravityTrayTarget != .ide {
                if antigravityTrayModels != .claudeGpt { slots.append(.agCliGemini) }
                if antigravityTrayModels != .gemini { slots.append(.agCliClaude) }
            }
            if antigravityTrayTarget != .cli {
                if antigravityTrayModels != .claudeGpt { slots.append(.agIdeGemini) }
                if antigravityTrayModels != .gemini { slots.append(.agIdeClaude) }
            }
        }
        return slots.isEmpty ? [.codex, .agCliGemini, .agCliClaude, .agIdeGemini, .agIdeClaude] : slots
    }
}

// MARK: - Status controller (engine orchestration, serial per-kind state machine)

final class StatusController {
    let state: AppState
    private let engine = EngineClient()

    private var statusInFlight = false
    private var autoswitchInFlight = false
    private var lastKnownSlot: Int?
    /// While this deadline is in the future, slot changes are considered app-initiated
    /// (manual switch or autoswitch-check) and do not trigger the "external change" notification.
    private var suppressExternalChangeUntil = Date.distantPast
    /// Slot the app was switching to when the engine watchdog fired. rotator.py swaps
    /// auth.json early in its sequence, so a timed-out switch has usually already
    /// succeeded — the next status confirms or denies instead of a hard error.
    private var pendingTimedOutSwitchSlot: Int?
    private var lastAutoswitchPromptAt = Date.distantPast

    init(state: AppState) {
        self.state = state
    }

    private var lastRefreshTime: Date?

    func refreshStatus(silent: Bool = false) {
        guard !statusInFlight else { return }
        statusInFlight = true
        if !silent {
            state.isLoading = true
        }
        engine.run(["status"], as: StatusResponse.self) { [weak self] result in
            guard let self = self else { return }
            self.statusInFlight = false
            self.lastRefreshTime = Date()
            if !silent {
                self.state.isLoading = false
            }
            switch result {
            case .failure(let error):
                self.state.engineMissing = error.isEngineMissing
                self.state.statusFailed = true
                self.state.lastErrorText = error.displayText
            case .success(let response):
                self.state.engineMissing = false
                self.state.statusFailed = (response.ok == false)
                self.state.lastErrorText = (response.ok == false) ? (response.error ?? (LanguageManager.shared.isRussian ? "Ошибка движка" : "Engine error")) : nil
                self.apply(status: response)
            }
        }
    }

    func refreshIfNeeded(minInterval: TimeInterval = 15, silent: Bool = true) {
        if state.status != nil, let last = lastRefreshTime, Date().timeIntervalSince(last) < minInterval {
            return
        }
        refreshStatus(silent: silent)
    }

    private func apply(status: StatusResponse) {
        if let running = status.daemon?.running {
            state.daemonRunning = running
        }
        if let autoswitch = status.autoswitch {
            if let enabled = autoswitch.enabled { state.autoswitchEnabled = enabled }
        }
        if let newSlot = status.active_slot {
            if let pendingSlot = pendingTimedOutSwitchSlot {
                // A switch timed out earlier — this status is the verification.
                pendingTimedOutSwitchSlot = nil
                if pendingSlot == newSlot {
                    state.lastErrorText = nil
                    let email = status.accounts?.first(where: { $0.slot == newSlot })?.email
                    postNotificationIfEnabled(L10n.switchedTo(email: email ?? (LanguageManager.shared.isRussian ? "слот \(newSlot)" : "slot \(newSlot)")))
                } else {
                    state.lastErrorText = LanguageManager.shared.isRussian
                        ? "Переключение на слот \(pendingSlot) не подтвердилось"
                        : "Switch to slot \(pendingSlot) was not confirmed"
                }
            } else if let previousSlot = lastKnownSlot,
                      previousSlot != newSlot,
                      Date() > suppressExternalChangeUntil {
                // External change (e.g. the reactive daemon rotated the account).
                let email = status.accounts?.first(where: { $0.slot == newSlot })?.email
                postNotificationIfEnabled(L10n.switchedTo(email: email ?? (LanguageManager.shared.isRussian ? "слот \(newSlot)" : "slot \(newSlot)")))
            }
            lastKnownSlot = newSlot
        }
        state.status = status
    }

    // MARK: Manual switching

    func switchTo(slot: Int, restartCodex: Bool = true) {
        guard !state.isSwitching else { return }
        state.isSwitching = true
        state.switchingSlot = slot
        markAppInitiatedChange()
        var args = ["switch", String(slot)]
        if !restartCodex { args.append("--no-restart") }
        engine.run(args, as: SwitchResponse.self) { [weak self] result in
            guard let self = self else { return }
            self.state.isSwitching = false
            self.state.switchingSlot = nil
            switch result {
            case .failure(let error):
                self.state.engineMissing = error.isEngineMissing
                if error.isTimedOut {
                    // auth.json is swapped early in rotator.py, so the switch likely
                    // already succeeded — verify via the forced refresh below.
                    self.pendingTimedOutSwitchSlot = slot
                    self.state.lastErrorText = LanguageManager.shared.isRussian
                        ? "Переключение затянулось — проверяю результат…"
                        : "Switching is taking longer — verifying..."
                } else {
                    self.state.lastErrorText = error.displayText
                }
            case .success(let response):
                if response.ok == true {
                    let emailText = response.email ?? (LanguageManager.shared.isRussian ? "слот \(slot)" : "slot \(slot)")
                    self.postNotificationIfEnabled(L10n.switchedTo(email: emailText, withoutRestart: !restartCodex))
                } else {
                    self.state.lastErrorText = response.error ?? L10n.failedToSwitch
                }
            }
            self.refreshStatus()
        }
    }

    // MARK: Re-authentication (interactive `codex login` for a dead slot)

    func reloginSlot(slot: Int) {
        guard !state.isSwitching, state.reloginingSlot == nil else { return }
        state.isSwitching = true
        state.reloginingSlot = slot
        // Isolated browser login can run for several minutes; suppress watcher
        // noise for the whole window while the engine owns this operation.
        markAppInitiatedChange(seconds: 330 + 30)
        engine.run(["relogin", String(slot)], as: SwitchResponse.self) { [weak self] result in
            guard let self = self else { return }
            self.state.isSwitching = false
            self.state.reloginingSlot = nil
            switch result {
            case .failure(let error):
                self.state.engineMissing = error.isEngineMissing
                self.state.lastErrorText = error.isTimedOut
                    ? L10n.loginTimedOut
                    : error.displayText
            case .success(let response):
                if response.ok == true {
                    let emailText = response.email ?? (LanguageManager.shared.isRussian ? "слот \(slot)" : "slot \(slot)")
                    self.postNotificationIfEnabled(LanguageManager.shared.isRussian ? "Авторизация обновлена: \(emailText)" : "Authorization updated: \(emailText)")
                } else {
                    self.state.lastErrorText = response.error ?? L10n.failedToUpdateAuth
                }
            }
            self.refreshStatus()
        }
    }

    // MARK: Config

    func loadConfig() {
        engine.run(["config", "get"], as: ConfigResponse.self) { [weak self] result in
            if case .success(let response) = result {
                self?.applyConfig(response.config)
            }
        }
    }

    private func applyConfig(_ config: ConfigData?) {
        guard let config = config else { return }
        state.config = config
    }

    func setTrayDisplay(_ display: TrayDisplay) {
        state.config?.tray_display = display.rawValue
        engine.run(["config", "set", "tray_display", display.rawValue], as: ConfigResponse.self) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let response) where response.ok == true:
                self.applyConfig(response.config)
            case .success(let response):
                self.state.lastErrorText = response.error ?? L10n.operationFailed
            case .failure(let error):
                self.state.lastErrorText = error.displayText
            }
        }
    }

    func setAntigravityTrayTarget(_ target: AntigravityTrayTarget) {
        state.config?.antigravity_tray_target = target.rawValue
        engine.run(["config", "set", "antigravity_tray_target", target.rawValue], as: ConfigResponse.self) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let response) where response.ok == true:
                self.applyConfig(response.config)
            case .success(let response):
                self.state.lastErrorText = response.error ?? L10n.operationFailed
            case .failure(let error):
                self.state.lastErrorText = error.displayText
            }
        }
    }

    func setAntigravityTrayModels(_ models: AntigravityTrayModels) {
        state.config?.antigravity_tray_models = models.rawValue
        engine.run(["config", "set", "antigravity_tray_models", models.rawValue], as: ConfigResponse.self) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let response) where response.ok == true:
                self.applyConfig(response.config)
            case .success(let response):
                self.state.lastErrorText = response.error ?? L10n.operationFailed
            case .failure(let error):
                self.state.lastErrorText = error.displayText
            }
        }
    }

    func setTraySlots(_ slots: [TraySlotItem]) {
        let raw = slots.map(\.rawValue)
        state.config?.tray_slots = raw
        let jsonStr: String
        if let data = try? JSONEncoder().encode(raw), let str = String(data: data, encoding: .utf8) {
            jsonStr = str
        } else {
            jsonStr = "[\(raw.map { "\"\($0)\"" }.joined(separator: ","))]"
        }
        engine.run(["config", "set", "tray_slots", jsonStr], as: ConfigResponse.self) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let response) where response.ok == true:
                self.applyConfig(response.config)
            case .success(let response):
                self.state.lastErrorText = response.error ?? L10n.operationFailed
            case .failure(let error):
                self.state.lastErrorText = error.displayText
            }
        }
    }

    // MARK: Login item (SMAppService)

    func refreshLoginItemState() {
        guard Bundle.main.bundleIdentifier != nil else { return }
        state.loginItemEnabled = (SMAppService.mainApp.status == .enabled)
    }

    func setLoginItem(_ enabled: Bool) {
        guard Bundle.main.bundleIdentifier != nil else { return }
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            state.lastErrorText = "Автозапуск: \(error.localizedDescription)"
        }
        refreshLoginItemState()
    }

    // MARK: Account Add / Delete

    func addAccount() {
        guard !state.isSwitching && !state.isAdding else { return }
        state.isAdding = true
        state.isSwitching = true
        markAppInitiatedChange(seconds: 330 + 30)
        engine.run(["add"], as: SwitchResponse.self) { [weak self] result in
            guard let self = self else { return }
            self.state.isSwitching = false
            self.state.isAdding = false
            switch result {
            case .failure(let error):
                self.state.engineMissing = error.isEngineMissing
                self.state.lastErrorText = error.isTimedOut
                    ? L10n.loginTimedOut
                    : error.displayText
            case .success(let response):
                if response.ok == true {
                    let emailText = response.email ?? (LanguageManager.shared.isRussian ? "слот \(response.slot ?? 0)" : "slot \(response.slot ?? 0)")
                    self.postNotificationIfEnabled(L10n.addedAccount(email: emailText))
                } else {
                    self.state.lastErrorText = response.error ?? L10n.failedToAddAccount
                }
            }
            self.refreshStatus()
        }
    }

    func cancelAddAccount() {
        state.isAdding = false
        state.isSwitching = false
        // Engine kills only the isolated `codex login` child it spawned (pidfile),
        // never an unrelated `codex login` running in the user's terminal.
        engine.run(["cancel-add"], as: SwitchResponse.self) { [weak self] _ in
            self?.refreshStatus()
        }
    }

    func deleteSlot(slot: Int, email: String?) {
        guard !state.isSwitching, state.deletingSlot == nil else { return }
        state.isSwitching = true
        state.deletingSlot = slot
        markAppInitiatedChange()
        engine.run(["delete", String(slot)], as: DaemonResponse.self) { [weak self] result in
            guard let self = self else { return }
            self.state.isSwitching = false
            self.state.deletingSlot = nil
            switch result {
            case .failure(let error):
                self.state.engineMissing = error.isEngineMissing
                self.state.lastErrorText = error.displayText
            case .success(let response):
                if response.ok == true {
                    let emailText = email ?? (LanguageManager.shared.isRussian ? "слот \(slot)" : "slot \(slot)")
                    self.postNotificationIfEnabled(L10n.deletedAccount(email: emailText))
                } else {
                    self.state.lastErrorText = response.error ?? L10n.failedToDeleteAccount
                }
            }
            self.refreshStatus()
        }
    }

    // MARK: Helpers

    func markAppInitiatedChange(seconds: TimeInterval = EngineClient.switchTimeout + 30) {
        // Comfortably longer than the engine watchdog for the operation, so a slow
        // (or timed-out-but-successful) change never reads as an external change.
        suppressExternalChangeUntil = Date().addingTimeInterval(seconds)
    }

    private func postNotificationIfEnabled(_ body: String) {
        guard state.notificationsEnabled else { return }
        Notifier.post(body: body)
    }
}

// MARK: - auth.json watcher (DispatchSource, re-opens fd on rename/delete)

final class AuthFileWatcher {
    private let path: String
    private var source: DispatchSourceFileSystemObject?
    private var debounceWork: DispatchWorkItem?
    private var retryDelay: TimeInterval = 10
    var onChange: (() -> Void)?

    init(path: String) {
        self.path = path
    }

    func start() {
        openAndWatch()
    }

    func stop() {
        debounceWork?.cancel()
        source?.cancel()
        source = nil
    }

    private func openAndWatch() {
        let descriptor = open(path, O_EVTONLY)
        guard descriptor >= 0 else {
            // File may not exist yet — retry with capped backoff.
            DispatchQueue.main.asyncAfter(deadline: .now() + retryDelay) { [weak self] in
                self?.retryDelay = min((self?.retryDelay ?? 10) * 2, 60)
                self?.openAndWatch()
            }
            return
        }
        retryDelay = 10
        let newSource = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor,
            eventMask: [.write, .rename, .delete],
            queue: .main
        )
        newSource.setEventHandler { [weak self] in
            guard let self = self, let activeSource = self.source else { return }
            let events = activeSource.data
            self.scheduleChange()
            if events.contains(.rename) || events.contains(.delete) {
                // rotator.py replaces the file atomically — re-open the descriptor.
                self.reopen()
            }
        }
        newSource.setCancelHandler {
            close(descriptor)
        }
        source = newSource
        newSource.resume()
    }

    private func reopen() {
        source?.cancel()
        source = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.openAndWatch()
        }
    }

    private func scheduleChange() {
        debounceWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.onChange?()
        }
        debounceWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0, execute: work)
    }
}

// MARK: - Time formatting helpers

func formatResetInterval(secondsFromNow seconds: Int) -> String {
    L10n.resetInterval(secondsFromNow: seconds)
}

func formatResetClock(timestamp: Int) -> String {
    L10n.resetClock(timestamp: timestamp)
}

func clampedPercent(_ value: Double?) -> Double {
    min(max(value ?? 0, 0), 100)
}

func remainingLimitPercent(fromUsed value: Double?) -> Double {
    max(0, 100 - clampedPercent(value))
}

func shortAccountName(email: String?, slot: Int?) -> String {
    guard let email,
          let local = email.split(separator: "@", maxSplits: 1).first,
          !local.isEmpty else {
        return slot.map { LanguageManager.shared.isRussian ? "слот\($0)" : "slot\($0)" } ?? "KS"
    }
    // First and last character of the local-part (before @).
    if local.count == 1 {
        return String(local)
    }
    return String(local.prefix(1)) + String(local.suffix(1))
}

func menuUsageWindows(_ usage: UsageInfo?) -> (short: UsageWindow?, weekly: UsageWindow?) {
    guard let usage else { return (nil, nil) }

    var short = usage.primary
    var weekly = usage.secondary

    if let s = short, let sMin = s.window_minutes, sMin > 24 * 60, weekly == nil {
        weekly = s
        short = nil
    } else if let w = weekly, let wMin = w.window_minutes, wMin <= 24 * 60, short == nil {
        short = w
        weekly = nil
    }

    return (short, weekly)
}

func antigravityUsageWindows(_ quota: AntigravityQuota?) -> (short: Double?, weekly: Double?, allowed: Bool) {
    guard let quota else { return (nil, nil, true) }

    let validGemini = (quota.gemini?.ok == true && quota.gemini?.stale != true) ? quota.gemini : nil
    let validThirdParty = (quota.thirdParty?.ok == true && quota.thirdParty?.stale != true) ? quota.thirdParty : nil

    let gWindows = menuUsageWindows(validGemini)
    let tWindows = menuUsageWindows(validThirdParty)

    let gAllowed = validGemini?.allowed != false
    let tAllowed = validThirdParty?.allowed != false
    let allowed = gAllowed && tAllowed

    var shortUsed: Double? = nil
    if let gShort = gWindows.short?.used_percent, let tShort = tWindows.short?.used_percent {
        shortUsed = max(gShort, tShort)
    } else {
        shortUsed = gWindows.short?.used_percent ?? tWindows.short?.used_percent
    }

    var weeklyUsed: Double? = nil
    if let gWeekly = gWindows.weekly?.used_percent, let tWeekly = tWindows.weekly?.used_percent {
        weeklyUsed = max(gWeekly, tWeekly)
    } else {
        weeklyUsed = gWindows.weekly?.used_percent ?? tWindows.weekly?.used_percent
    }

    return (shortUsed, weeklyUsed, allowed)
}

func isLimitWindowExhausted(_ window: UsageWindow?) -> Bool {
    clampedPercent(window?.used_percent) >= 99
}

// MARK: - SwiftUI: usage bar row

struct UsageBarRow: View {
    let label: String
    let window: UsageWindow
    let stale: Bool

    private var usedPercent: Double {
        clampedPercent(window.used_percent)
    }

    private var remainingPercent: Double {
        remainingLimitPercent(fromUsed: window.used_percent)
    }

    private var barColor: Color {
        let value = usedPercent
        if value >= 90 { return .red }
        if value >= 75 { return .orange }
        if value >= 50 { return .yellow }
        return .green
    }

    private var percentText: String {
        "\(remainingInt)%"
    }

    private var remainingInt: Int {
        Int(remainingPercent.rounded())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(label)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .frame(width: 44, alignment: .leading)
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
                GeometryReader { geometry in
                    ZStack(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 4)
                            .fill(Color.primary.opacity(0.10))
                        RoundedRectangle(cornerRadius: 4)
                            .fill(barColor)
                            .frame(width: max(0, geometry.size.width * CGFloat(usedPercent) / 100))
                    }
                }
                .frame(height: 8)
                Text(percentText)
                    .font(.caption)
                    .monospacedDigit()
                    .frame(width: 34, alignment: .trailing)
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
            }
            if !resetText.isEmpty {
                Text(resetText)
                    .font(.system(size: 10))
                    .foregroundColor(.secondary)
                    .padding(.leading, 50)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
        }
    }

    private var resetText: String {
        var text = ""
        if let resetAt = window.reset_at {
            let remaining = resetAt - Int(Date().timeIntervalSince1970)
            text = "\(formatResetInterval(secondsFromNow: remaining)) (\(formatResetClock(timestamp: resetAt)))"
        }
        return text
    }
}

struct HoverIcon: View {
    let systemName: String
    var showsProgress = false
    var isHovered = false
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        Group {
            if showsProgress {
                ProgressView()
                    .controlSize(.small)
            } else {
                Image(systemName: systemName)
                    .font(.system(size: 13, weight: .medium))
            }
        }
        .foregroundColor(isHovered && isEnabled ? .accentColor : .primary)
        .frame(width: 24, height: 24)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(isHovered && isEnabled ? Color.accentColor.opacity(0.28) : .clear)
        )
        .contentShape(Rectangle())
    }
}

struct HoverTrackingView: NSViewRepresentable {
    let onHover: (Bool) -> Void

    func makeNSView(context: Context) -> HoverTrackingNSView {
        let view = HoverTrackingNSView()
        view.onHover = onHover
        return view
    }

    func updateNSView(_ nsView: HoverTrackingNSView, context: Context) {
        nsView.onHover = onHover
    }
}

final class HoverTrackingNSView: NSView {
    var onHover: ((Bool) -> Void)?
    private var hoverTrackingArea: NSTrackingArea?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let hoverTrackingArea {
            removeTrackingArea(hoverTrackingArea)
        }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
        hoverTrackingArea = area
    }

    override func mouseEntered(with event: NSEvent) {
        onHover?(true)
    }

    override func mouseExited(with event: NSEvent) {
        onHover?(false)
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        nil
    }
}

// MARK: - SwiftUI: account card

struct AccountCardView: View {
    let account: AccountInfo
    let isSwitchingThis: Bool
    let isReloginingThis: Bool
    let buttonsDisabled: Bool
    let onSwitchWithRestart: () -> Void
    let onRelogin: () -> Void
    let onDelete: () -> Void
    let onDragChanged: (DragGesture.Value) -> Void
    let onDragEnded: () -> Void
    @State private var isDragHandleHovered = false
    @State private var isActionsHovered = false
    @State private var isEmailRevealed = false
    @State private var isEmailHovered = false

    private var isActive: Bool { account.active == true }

    private var errorKind: String? {
        account.error ?? account.usage?.error
    }

    private var accountLabel: String {
        shortAccountName(email: account.email, slot: account.slot)
    }

    private var displayedAccountName: String {
        if isEmailRevealed, let email = account.email, !email.isEmpty {
            return email
        }
        return accountLabel
    }

    /// A non-active slot whose saved session is dead and needs re-login.
    private var needsLogin: Bool {
        guard !isActive else { return false }
        if errorKind == "auth_expired" { return true }
        // Any non-active slot we genuinely failed to read usage for (and aren't
        // just serving stale data) is treated as needing a fresh sign-in.
        if let usage = account.usage {
            return usage.ok != true && usage.stale != true
        }
        return false
    }

    private var strokeColor: Color {
        if isLimitExhausted { return Color.red.opacity(0.75) }
        if isActive { return Color.green.opacity(0.65) }
        if needsLogin { return Color.red.opacity(0.5) }
        return Color.clear
    }

    private var isLimitExhausted: Bool {
        guard let usage = account.usage, usage.ok == true else { return false }
        if usage.allowed == false { return true }
        return isLimitWindowExhausted(usage.primary) || isLimitWindowExhausted(usage.secondary)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            headerRow
            usageSection
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(strokeColor, lineWidth: 1.5)
        )
    }

    private var headerRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "line.3.horizontal")
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(isDragHandleHovered ? .accentColor : Color.primary.opacity(0.8))
                .frame(width: 18, height: 22)
                .background(
                    RoundedRectangle(cornerRadius: 5)
                        .fill(
                            isDragHandleHovered
                                ? Color.accentColor.opacity(0.28)
                                : Color.primary.opacity(0.10)
                        )
                )
                .contentShape(Rectangle())
                .onHover { hovering in
                    withAnimation(.easeOut(duration: 0.1)) {
                        isDragHandleHovered = hovering
                    }
                    hovering ? NSCursor.openHand.set() : NSCursor.arrow.set()
                }
                .gesture(
                    DragGesture(minimumDistance: 2, coordinateSpace: .named("accountList"))
                        .onChanged(onDragChanged)
                        .onEnded { _ in onDragEnded() }
                )
                .accessibilityLabel(L10n.reorderAccount)
                .accessibilityHint(L10n.dragHint)
            if isActive {
                Circle()
                    .fill(Color.green)
                    .frame(width: 10, height: 10)
            } else if needsLogin {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundColor(.red)
            }
            if isLimitExhausted {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundColor(.red)
            }
            Text(displayedAccountName)
                .font(.callout)
                .fontWeight(isActive ? .bold : .regular)
                .lineLimit(1)
                .truncationMode(.middle)
                .contentShape(Rectangle())
                .onHover { hovering in
                    isEmailHovered = hovering
                    if hovering {
                        NSCursor.pointingHand.set()
                    } else {
                        NSCursor.arrow.set()
                    }
                }
                .onTapGesture {
                    withAnimation(.easeInOut(duration: 0.15)) {
                        isEmailRevealed.toggle()
                    }
                }
                .help(isEmailRevealed ? L10n.hideFullEmail : L10n.showFullEmail)
            if let plan = account.plan, !plan.isEmpty {
                Text(plan.capitalized)
                    .font(.caption2)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(Color.accentColor.opacity(0.18)))
                    .foregroundColor(.accentColor)
            }
            Spacer(minLength: 4)
            if isActive {
                Text(L10n.active)
                    .font(.caption)
                    .foregroundColor(.green)
            }
            actionControl
        }
    }

    @ViewBuilder
    private var actionControl: some View {
        HStack(spacing: 6) {
            if isReloginingThis {
                HStack(spacing: 4) {
                    ProgressView().controlSize(.small)
                    Text(L10n.openingLogin)
                        .font(.caption2)
                        .foregroundColor(.secondary)
                }
            } else if isSwitchingThis {
                ProgressView()
                    .controlSize(.small)
            } else if needsLogin {
                Button(action: onRelogin) {
                    Label(L10n.logIn, systemImage: "person.crop.circle.badge.plus")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .tint(.red)
                .disabled(buttonsDisabled)
                .nonFocusable()
            }

            if !isReloginingThis && !isSwitchingThis {
                ZStack {
                    Menu {
                        if !isActive && !needsLogin {
                            Button(action: onSwitchWithRestart) {
                                Label(L10n.switchAction, systemImage: "arrow.triangle.2.circlepath")
                            }
                            Divider()
                        }
                        Button(L10n.deleteAccount, role: .destructive, action: onDelete)
                    } label: {
                        HoverIcon(systemName: "ellipsis", isHovered: isActionsHovered)
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .controlSize(.small)
                    .disabled(buttonsDisabled)
                    .accessibilityLabel(L10n.accountActions)
                    .nonFocusable()
                }
                .contentShape(Rectangle())
                .overlay {
                    HoverTrackingView { isActionsHovered = $0 }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
    }

    @ViewBuilder
    private var usageSection: some View {
        if let usage = account.usage, usage.ok == true {
            let windows = menuUsageWindows(usage)
            VStack(alignment: .leading, spacing: 4) {
                if let primary = windows.short {
                    UsageBarRow(label: L10n.fiveHours, window: primary, stale: usage.stale ?? false)
                }
                if let secondary = windows.weekly {
                    UsageBarRow(label: L10n.week, window: secondary, stale: usage.stale ?? false)
                }
                if windows.short == nil && windows.weekly == nil {
                    noDataText
                }
            }
        } else {
            errorRow
        }
    }

    @ViewBuilder
    private var errorRow: some View {
        if errorKind == "auth_expired" {
            Text(L10n.sessionExpired)
                .font(.caption)
                .foregroundColor(.red)
        } else {
            noDataText
        }
    }

    private var noDataText: some View {
        Text(L10n.noData)
            .font(.caption)
            .foregroundColor(.secondary)
    }
}

private struct AccountFramePreferenceKey: PreferenceKey {
    static var defaultValue: [Int: CGRect] = [:]

    static func reduce(value: inout [Int: CGRect], nextValue: () -> [Int: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

// MARK: - SwiftUI: popover panel

extension View {
    /// Suppresses the blue keyboard-focus ring AppKit draws on controls when the
    /// popover is the key window and the user tabs through it. macOS 14+ uses the
    /// native modifier; older systems keep the default behavior.
    @ViewBuilder
    func noFocusRing() -> some View {
        if #available(macOS 14.0, *) {
            focusEffectDisabled()
        } else {
            self
        }
    }

    /// Prevents buttons in the popover from stealing initial keyboard focus.
    @ViewBuilder
    func nonFocusable() -> some View {
        if #available(macOS 13.0, *) {
            focusable(false)
        } else {
            self
        }
    }
}

// MARK: - Menu Bar Customization Modal

private struct TraySlotFramePreferenceKey: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]

    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

struct TrayCustomizationModal: View {
    @ObservedObject var state: AppState
    let controller: StatusController
    @ObservedObject var antigravityController: AntigravityController
    var onClose: (() -> Void)? = nil

    @State private var draggedSlotID: String? = nil
    @State private var dragOffset: CGFloat = 0
    @State private var dropTargetID: String? = nil
    @State private var dropTargetEdge: VerticalEdge? = nil
    @State private var slotFrames: [String: CGRect] = [:]

    private var activeSlots: [TraySlotItem] {
        state.traySlots
    }

    private var availableSlots: [TraySlotItem] {
        TraySlotItem.allCases.filter { !activeSlots.contains($0) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            // Header
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(L10n.trayModalTitle)
                        .font(.headline)
                    Text(L10n.trayModalSubtitle)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                if let onClose = onClose {
                    Button(action: onClose) {
                        Image(systemName: "xmark.circle.fill")
                            .font(.title3)
                            .foregroundColor(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(L10n.done)
                }
            }

            Divider()

            // Live Preview Box
            VStack(alignment: .leading, spacing: 6) {
                Text(L10n.trayPreviewLabel)
                    .font(.caption2)
                    .fontWeight(.semibold)
                    .foregroundColor(.secondary)

                livePreviewBar
            }

            Divider()

            // Active Slots Section
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(L10n.activeTraySlots)
                        .font(.subheadline)
                        .fontWeight(.semibold)
                    Text("(\(activeSlots.count))")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Spacer()
                }

                if activeSlots.isEmpty {
                    Text(L10n.emptyTrayHint)
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .italic()
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.vertical, 16)
                        .background(
                            RoundedRectangle(cornerRadius: 8)
                                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4]))
                                .foregroundColor(Color.secondary.opacity(0.3))
                        )
                } else {
                    activeSlotsList
                }
            }

            // Available Slots to Add
            if !availableSlots.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text(L10n.availableTrayEntities)
                        .font(.subheadline)
                        .fontWeight(.semibold)

                    VStack(spacing: 6) {
                        ForEach(availableSlots) { slot in
                            availableSlotRow(slot)
                        }
                    }
                }
            }

            Divider()

            // Footer
            HStack {
                Button(L10n.resetToDefault) {
                    controller.setTraySlots([.codex, .agCliGemini, .agCliClaude, .agIdeGemini, .agIdeClaude])
                }
                .buttonStyle(.borderless)
                .font(.caption)
                .foregroundColor(.secondary)

                Spacer()

                if let onClose = onClose {
                    Button(L10n.done) {
                        onClose()
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.regular)
                }
            }
        }
        .padding(18)
        .frame(width: 520)
    }

    private var livePreviewBar: some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.triangle.2.circlepath.circle.fill")
                .foregroundColor(.accentColor)
                .font(.system(size: 13))

            if activeSlots.isEmpty {
                Text(L10n.emptyTrayHint)
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .italic()
            } else {
                ForEach(Array(activeSlots.enumerated()), id: \.element.id) { index, slot in
                    if index > 0 {
                        Text("|")
                            .font(.caption2)
                            .foregroundColor(.secondary.opacity(0.6))
                    }
                    previewSlotItem(slot)
                }
            }

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.primary.opacity(0.12), lineWidth: 1)
        )
    }

    @ViewBuilder
    private func previewSlotItem(_ slot: TraySlotItem) -> some View {
        switch slot {
        case .codex:
            let activeAccount = state.status?.accounts?.first(where: { $0.active == true })
                ?? state.status?.accounts?.first(where: { $0.slot == state.status?.active_slot })
            let name = shortAccountName(email: activeAccount?.email, slot: state.status?.active_slot)
            let windows = menuUsageWindows(activeAccount?.usage)

            HStack(spacing: 4) {
                Text(name)
                    .font(.caption2)
                    .fontWeight(.medium)
                if let short = windows.short?.used_percent {
                    let remaining = Int(remainingLimitPercent(fromUsed: short).rounded())
                    Image(systemName: "clock")
                        .font(.system(size: 9))
                    Text("\(remaining)%")
                        .font(.caption2)
                }
                if let weekly = windows.weekly?.used_percent {
                    let remaining = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                    Image(systemName: "calendar")
                        .font(.system(size: 9))
                    Text("\(remaining)%")
                        .font(.caption2)
                }
            }

        case .agCliGemini, .agCliClaude:
            let hasIde = activeSlots.contains { $0 == .agIdeGemini || $0 == .agIdeClaude }
            let prefix = hasIde ? "CLI: " : ""
            let activeID = antigravityController.status?.active?["cli"]
            let profile = antigravityController.status?.profiles?.first(where: { $0.id == activeID })
                ?? antigravityController.status?.profiles?.first
            let name = prefix + shortAccountName(email: profile?.email, slot: nil)
            let quota = profile?.quota

            HStack(spacing: 4) {
                Text(name)
                    .font(.caption2)
                    .fontWeight(.medium)

                if slot == .agCliGemini {
                    let gUsage = (quota?.gemini?.ok == true && quota?.gemini?.stale != true) ? quota?.gemini : nil
                    let gWindows = menuUsageWindows(gUsage)
                    Image(systemName: "sparkle")
                        .font(.system(size: 9))
                    if let short = gWindows.short?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: short).rounded())
                        Text("\(rem)%").font(.caption2)
                    } else if let weekly = gWindows.weekly?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                        Text("\(rem)%").font(.caption2)
                    }
                } else {
                    let tUsage = (quota?.thirdParty?.ok == true && quota?.thirdParty?.stale != true) ? quota?.thirdParty : nil
                    let tWindows = menuUsageWindows(tUsage)
                    Image(systemName: "bolt.fill")
                        .font(.system(size: 9))
                    if let short = tWindows.short?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: short).rounded())
                        Text("\(rem)%").font(.caption2)
                    } else if let weekly = tWindows.weekly?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                        Text("\(rem)%").font(.caption2)
                    }
                }
            }

        case .agIdeGemini, .agIdeClaude:
            let hasCli = activeSlots.contains { $0 == .agCliGemini || $0 == .agCliClaude }
            let prefix = hasCli ? "IDE: " : ""
            let activeID = antigravityController.status?.active?["ide"]
            let profile = antigravityController.status?.profiles?.first(where: { $0.id == activeID })
                ?? antigravityController.status?.profiles?.first
            let name = prefix + shortAccountName(email: profile?.email, slot: nil)
            let quota = profile?.quota

            HStack(spacing: 4) {
                Text(name)
                    .font(.caption2)
                    .fontWeight(.medium)

                if slot == .agIdeGemini {
                    let gUsage = (quota?.gemini?.ok == true && quota?.gemini?.stale != true) ? quota?.gemini : nil
                    let gWindows = menuUsageWindows(gUsage)
                    Image(systemName: "sparkle")
                        .font(.system(size: 9))
                    if let short = gWindows.short?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: short).rounded())
                        Text("\(rem)%").font(.caption2)
                    } else if let weekly = gWindows.weekly?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                        Text("\(rem)%").font(.caption2)
                    }
                } else {
                    let tUsage = (quota?.thirdParty?.ok == true && quota?.thirdParty?.stale != true) ? quota?.thirdParty : nil
                    let tWindows = menuUsageWindows(tUsage)
                    Image(systemName: "bolt.fill")
                        .font(.system(size: 9))
                    if let short = tWindows.short?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: short).rounded())
                        Text("\(rem)%").font(.caption2)
                    } else if let weekly = tWindows.weekly?.used_percent {
                        let rem = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                        Text("\(rem)%").font(.caption2)
                    }
                }
            }
        }
    }

    private var activeSlotsList: some View {
        VStack(spacing: 6) {
            ForEach(activeSlots) { slot in
                activeSlotRow(slot)
                    .background {
                        GeometryReader { geometry in
                            Color.clear.preference(
                                key: TraySlotFramePreferenceKey.self,
                                value: [slot.id: geometry.frame(in: .named("traySlotList"))]
                            )
                        }
                    }
                    .offset(y: draggedSlotID == slot.id ? dragOffset : 0)
                    .scaleEffect(draggedSlotID == slot.id ? 1.015 : 1)
                    .shadow(
                        color: .black.opacity(draggedSlotID == slot.id ? 0.24 : 0),
                        radius: draggedSlotID == slot.id ? 12 : 0,
                        y: draggedSlotID == slot.id ? 6 : 0
                    )
                    .zIndex(draggedSlotID == slot.id ? 10 : 0)
                    .overlay(alignment: dropIndicatorAlignment) {
                        dropIndicator(for: slot.id)
                    }
                    .accessibilityAction(named: Text(L10n.moveUp)) {
                        moveSlot(slot, by: -1)
                    }
                    .accessibilityAction(named: Text(L10n.moveDown)) {
                        moveSlot(slot, by: 1)
                    }
            }
        }
        .coordinateSpace(name: "traySlotList")
        .onPreferenceChange(TraySlotFramePreferenceKey.self) { frames in
            if draggedSlotID == nil {
                slotFrames = frames
            }
        }
    }

    private var dropIndicatorAlignment: Alignment {
        dropTargetEdge == .bottom ? .bottom : .top
    }

    @ViewBuilder
    private func dropIndicator(for slotID: String) -> some View {
        if dropTargetID == slotID, let edge = dropTargetEdge {
            Rectangle()
                .fill(Color.accentColor)
                .frame(height: 2)
                .padding(.horizontal, 4)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: edge == .top ? .top : .bottom)
                .allowsHitTesting(false)
        }
    }

    private func activeSlotRow(_ slot: TraySlotItem) -> some View {
        HStack(spacing: 10) {
            // Drag handle
            Image(systemName: "line.3.horizontal")
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(Color.primary.opacity(0.7))
                .frame(width: 20, height: 26)
                .background(
                    RoundedRectangle(cornerRadius: 5)
                        .fill(Color.primary.opacity(0.08))
                )
                .contentShape(Rectangle())
                .gesture(
                    DragGesture(minimumDistance: 2, coordinateSpace: .named("traySlotList"))
                        .onChanged { updateDrag($0, slotID: slot.id) }
                        .onEnded { _ in finishDrag(slotID: slot.id) }
                )

            // Icon
            Image(systemName: slot.systemIcon)
                .font(.system(size: 14))
                .foregroundColor(slot.iconColor)
                .frame(width: 22)

            // Titles
            VStack(alignment: .leading, spacing: 1) {
                Text(slot.title)
                    .font(.callout)
                    .fontWeight(.medium)
                Text(slot.description)
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }

            Spacer()

            // Move Up/Down Buttons
            HStack(spacing: 2) {
                Button {
                    moveSlot(slot, by: -1)
                } label: {
                    Image(systemName: "chevron.up")
                        .font(.system(size: 10, weight: .semibold))
                }
                .buttonStyle(.plain)
                .disabled(activeSlots.first == slot)
                .opacity(activeSlots.first == slot ? 0.3 : 1)

                Button {
                    moveSlot(slot, by: 1)
                } label: {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                }
                .buttonStyle(.plain)
                .disabled(activeSlots.last == slot)
                .opacity(activeSlots.last == slot ? 0.3 : 1)
            }
            .padding(.trailing, 4)

            // Remove button
            Button {
                removeSlot(slot)
            } label: {
                Image(systemName: "minus.circle.fill")
                    .font(.system(size: 15))
                    .foregroundColor(.red.opacity(0.85))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(L10n.removeSlot)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.primary.opacity(0.08), lineWidth: 1)
        )
    }

    private func availableSlotRow(_ slot: TraySlotItem) -> some View {
        HStack(spacing: 10) {
            Image(systemName: slot.systemIcon)
                .font(.system(size: 14))
                .foregroundColor(slot.iconColor)
                .frame(width: 22)

            VStack(alignment: .leading, spacing: 1) {
                Text(slot.title)
                    .font(.callout)
                    .fontWeight(.medium)
                Text(slot.description)
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }

            Spacer()

            Button {
                addSlot(slot)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "plus.circle.fill")
                    Text(L10n.addSlot)
                        .font(.caption2)
                }
                .foregroundColor(.accentColor)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.primary.opacity(0.03))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [3]))
                .foregroundColor(Color.primary.opacity(0.12))
        )
    }

    private func addSlot(_ slot: TraySlotItem) {
        var slots = activeSlots
        if !slots.contains(slot) {
            slots.append(slot)
            controller.setTraySlots(slots)
        }
    }

    private func removeSlot(_ slot: TraySlotItem) {
        var slots = activeSlots
        slots.removeAll { $0 == slot }
        controller.setTraySlots(slots)
    }

    private func moveSlot(_ slot: TraySlotItem, by delta: Int) {
        var slots = activeSlots
        guard let idx = slots.firstIndex(of: slot) else { return }
        let newIdx = idx + delta
        guard newIdx >= 0, newIdx < slots.count else { return }
        slots.swapAt(idx, newIdx)
        controller.setTraySlots(slots)
    }

    private func toggleModel(gemini: Bool, claude: Bool) {
        if gemini && claude {
            controller.setAntigravityTrayModels(.both)
        } else if gemini {
            controller.setAntigravityTrayModels(.gemini)
        } else if claude {
            controller.setAntigravityTrayModels(.claudeGpt)
        } else {
            controller.setAntigravityTrayModels(.both)
        }
    }

    private func updateDrag(_ value: DragGesture.Value, slotID: String) {
        guard draggedSlotID == nil || draggedSlotID == slotID else { return }
        draggedSlotID = slotID
        dragOffset = value.translation.height

        guard let currentFrame = slotFrames[slotID] else { return }
        let currentMidY = currentFrame.midY + dragOffset
        var nearestID: String? = nil
        var nearestDistance: CGFloat = .infinity
        var edge: VerticalEdge = .top

        for (id, frame) in slotFrames where id != slotID {
            let distance = abs(frame.midY - currentMidY)
            if distance < nearestDistance {
                nearestDistance = distance
                nearestID = id
                edge = currentMidY < frame.midY ? .top : .bottom
            }
        }

        dropTargetID = nearestID
        dropTargetEdge = edge
    }

    private func finishDrag(slotID: String) {
        guard draggedSlotID == slotID else { return }
        let targetID = dropTargetID
        let targetEdge = dropTargetEdge

        withAnimation(.easeOut(duration: 0.15)) {
            draggedSlotID = nil
            dragOffset = 0
            dropTargetID = nil
            dropTargetEdge = nil
        }

        guard let targetID, targetID != slotID else { return }
        var slots = activeSlots
        guard let sourceIndex = slots.firstIndex(where: { $0.id == slotID }),
              let destIndex = slots.firstIndex(where: { $0.id == targetID }) else { return }

        let item = slots.remove(at: sourceIndex)
        var insertionIndex = destIndex
        if targetEdge == .bottom {
            insertionIndex = min(insertionIndex + 1, slots.count)
        }
        if sourceIndex < destIndex && targetEdge == .top {
            insertionIndex = max(0, insertionIndex)
        }
        insertionIndex = min(max(0, insertionIndex), slots.count)
        slots.insert(item, at: insertionIndex)

        controller.setTraySlots(slots)
    }
}

// MARK: - Standalone Window Controller for Menu Bar Settings

@MainActor
final class TraySettingsWindowController: NSObject, NSWindowDelegate {
    static let shared = TraySettingsWindowController()

    private var window: NSWindow?

    func show(state: AppState, controller: StatusController, antigravityController: AntigravityController) {
        AppDelegate.shared?.closePopover(nil)

        if let existing = window {
            existing.center()
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let contentView = TrayCustomizationModal(
            state: state,
            controller: controller,
            antigravityController: antigravityController,
            onClose: { [weak self] in
                self?.window?.close()
            }
        )

        let hostingController = NSHostingController(rootView: contentView)
        let newWindow = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 540, height: 500),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        newWindow.title = L10n.trayModalTitle
        newWindow.contentViewController = hostingController
        newWindow.isReleasedWhenClosed = false
        newWindow.delegate = self
        newWindow.center()
        newWindow.level = .floating
        self.window = newWindow

        newWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        window = nil
    }
}

struct PanelView: View {
    @ObservedObject var state: AppState
    let controller: StatusController
    @ObservedObject var antigravityController: AntigravityController
    @ObservedObject private var languageManager = LanguageManager.shared
    @State private var refreshCooldown = false
    @State private var isAddHovered = false
    @State private var isRefreshHovered = false
    @State private var slotToDelete: AccountInfo? = nil
    @State private var accountOrder: [Int] = {
        (UserDefaults.standard.array(forKey: "accountOrder") ?? []).compactMap {
            ($0 as? NSNumber)?.intValue
        }
    }()
    @State private var draggedSlot: Int? = nil
    @State private var dragOffset: CGFloat = 0
    @State private var dropTargetSlot: Int? = nil
    @State private var dropTargetEdge: VerticalEdge? = nil
    @State private var accountFrames: [Int: CGRect] = [:]
    @State private var isFooterSettingsHovered = false

    private var refreshDisabled: Bool {
        state.isLoading || state.isSwitching || refreshCooldown
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            codexPanel

            Divider()

            AntigravityPanelView(controller: antigravityController)

            Divider()

            footerView
        }
        .padding(12)
        .frame(width: 580)
        .alert(L10n.deleteAccountTitle, isPresented: Binding(
            get: { slotToDelete != nil },
            set: { if !$0 { slotToDelete = nil } }
        )) {
            if let slot = slotToDelete {
                Button(L10n.delete, role: .destructive) {
                    controller.deleteSlot(slot: slot.slot, email: slot.email)
                    slotToDelete = nil
                }
                Button(L10n.cancel, role: .cancel) {
                    slotToDelete = nil
                }
            }
        } message: {
            if let slot = slotToDelete {
                if slot.active == true {
                    Text(L10n.deleteCodexActiveMessage(email: slot.email ?? (LanguageManager.shared.isRussian ? "слот \(slot.slot)" : "slot \(slot.slot)")))
                } else {
                    Text(L10n.deleteCodexSlotMessage(email: slot.email ?? (LanguageManager.shared.isRussian ? "слот \(slot.slot)" : "slot \(slot.slot)")))
                }
            }
        }
    }

    private var footerView: some View {
        HStack {
            Text("KeySwitcher")
                .font(.caption2)
                .foregroundColor(.secondary)
            Spacer()
            ZStack {
                Menu {
                    Toggle(isOn: Binding(
                        get: { state.loginItemEnabled },
                        set: { controller.setLoginItem($0) }
                    )) {
                        Text(L10n.launchAtLogin)
                    }

                    Menu {
                        ForEach(AppLanguage.allCases) { lang in
                            Button {
                                languageManager.language = lang
                            } label: {
                                HStack {
                                    Text(lang.displayName)
                                    if languageManager.language == lang {
                                        Image(systemName: "checkmark")
                                    }
                                }
                            }
                        }
                    } label: {
                        Text(L10n.languageMenu)
                    }

                    Divider()

                    Button {
                        TraySettingsWindowController.shared.show(
                            state: state,
                            controller: controller,
                            antigravityController: antigravityController
                        )
                    } label: {
                        Label(L10n.customizeTrayMenu, systemImage: "slider.horizontal.3")
                    }

                    Divider()
                    Button(L10n.quit, role: .destructive) {
                        NSApp.terminate(nil)
                    }
                } label: {
                    HoverIcon(systemName: "gearshape", isHovered: isFooterSettingsHovered)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .controlSize(.small)
                .accessibilityLabel(L10n.settings)
                .nonFocusable()
            }
            .contentShape(Rectangle())
            .overlay {
                HoverTrackingView { isFooterSettingsHovered = $0 }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(.top, 2)
    }

    private var codexPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()
            content
            if let errorText = state.lastErrorText, !state.engineMissing {
                Text(errorText)
                    .font(.caption2)
                    .foregroundColor(.red)
                    .lineLimit(2)
            }
        }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 8) {
            Text(L10n.codexHeader)
                .font(.headline)
            Spacer()
            Button(action: { controller.addAccount() }) {
                HoverIcon(systemName: "plus", isHovered: isAddHovered)
            }
            .buttonStyle(.borderless)
            .disabled(state.isSwitching || state.isAdding)
            .accessibilityLabel(L10n.addAccount)
            .onHover { isAddHovered = $0 }
            .nonFocusable()
            Button {
                refreshStatus()
            } label: {
                HoverIcon(
                    systemName: "arrow.clockwise",
                    showsProgress: state.isLoading,
                    isHovered: isRefreshHovered
                )
            }
            .buttonStyle(.borderless)
            .disabled(refreshDisabled)
            .accessibilityLabel(L10n.refresh)
            .onHover { isRefreshHovered = $0 }
            .nonFocusable()
        }
    }

    // MARK: Accounts / empty states

    @ViewBuilder
    private var content: some View {
        if state.engineMissing {
            VStack(alignment: .leading, spacing: 4) {
                Text(L10n.engineMissingTitle)
                    .font(.callout)
                    .foregroundColor(.secondary)
                Text(L10n.engineMissingSubtitle)
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 12)
        } else if let accounts = state.status?.accounts, !accounts.isEmpty {
            if accounts.count > 3 {
                ScrollView {
                    codexAccountsList(accounts)
                        .padding(.horizontal, 2)
                        .padding(.vertical, 2)
                }
                .frame(height: 280)
            } else {
                codexAccountsList(accounts)
                    .padding(.horizontal, 2)
                    .padding(.vertical, 2)
            }
        } else if state.isLoading {
            Text(L10n.loading)
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 12)
        } else {
            VStack(spacing: 12) {
                Text(state.lastErrorText ?? L10n.noData)
                    .font(.callout)
                    .foregroundColor(.secondary)
                addAccountProgress
            }
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 12)
        }
    }

    // MARK: Add Account Progress

    @ViewBuilder
    private var addAccountProgress: some View {
        if state.isAdding {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(L10n.signingInBrowser)
                    .font(.callout)
                    .foregroundColor(.secondary)
                Spacer()
                Button(L10n.cancel) {
                    controller.cancelAddAccount()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .nonFocusable()
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 8)
        }
    }

    private func codexAccountsList(_ accounts: [AccountInfo]) -> some View {
        VStack(spacing: 8) {
            ForEach(orderedAccounts(accounts), id: \.slot) { account in
                AccountCardView(
                    account: account,
                    isSwitchingThis: state.switchingSlot == account.slot,
                    isReloginingThis: state.reloginingSlot == account.slot,
                    buttonsDisabled: state.isSwitching,
                    onSwitchWithRestart: { controller.switchTo(slot: account.slot, restartCodex: true) },
                    onRelogin: { controller.reloginSlot(slot: account.slot) },
                    onDelete: { slotToDelete = account },
                    onDragChanged: { updateDrag($0, slot: account.slot, accounts: accounts) },
                    onDragEnded: { finishDrag(slot: account.slot, accounts: accounts) }
                )
                .background {
                    GeometryReader { geometry in
                        Color.clear.preference(
                            key: AccountFramePreferenceKey.self,
                            value: [account.slot: geometry.frame(in: .named("accountList"))]
                        )
                    }
                }
                .offset(y: draggedSlot == account.slot ? dragOffset : 0)
                .scaleEffect(draggedSlot == account.slot ? 1.015 : 1)
                .shadow(
                    color: .black.opacity(draggedSlot == account.slot ? 0.24 : 0),
                    radius: draggedSlot == account.slot ? 12 : 0,
                    y: draggedSlot == account.slot ? 6 : 0
                )
                .zIndex(draggedSlot == account.slot ? 10 : 0)
                .overlay(alignment: dropIndicatorAlignment) {
                    dropIndicator(for: account.slot)
                }
                .accessibilityAction(named: Text(L10n.moveUp)) {
                    moveAccount(account.slot, by: -1, accounts: accounts)
                }
                .accessibilityAction(named: Text(L10n.moveDown)) {
                    moveAccount(account.slot, by: 1, accounts: accounts)
                }
            }
            addAccountProgress
        }
        .coordinateSpace(name: "accountList")
        .onAppear { syncAccountOrder(with: accounts) }
        .onChange(of: accounts.map(\.slot)) { _ in
            syncAccountOrder(with: accounts)
        }
        .onPreferenceChange(AccountFramePreferenceKey.self) { frames in
            if draggedSlot == nil {
                accountFrames = frames
            }
        }
    }

    private func refreshStatus() {
        guard !refreshDisabled else { return }
        refreshCooldown = true
        controller.refreshStatus(silent: false)
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            refreshCooldown = false
        }
    }

    private var dropIndicatorAlignment: Alignment {
        dropTargetEdge == .bottom ? .bottom : .top
    }

    @ViewBuilder
    private func dropIndicator(for slot: Int) -> some View {
        if dropTargetSlot == slot && draggedSlot != slot, let edge = dropTargetEdge {
            Rectangle()
                .fill(Color.accentColor)
                .frame(height: 2)
                .padding(.horizontal, 4)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: edge == .top ? .top : .bottom)
                .allowsHitTesting(false)
        }
    }

    private func orderedAccounts(_ accounts: [AccountInfo]) -> [AccountInfo] {
        let accountsBySlot = Dictionary(uniqueKeysWithValues: accounts.map { ($0.slot, $0) })
        return normalizedAccountOrder(for: accounts).compactMap { accountsBySlot[$0] }
    }

    private func normalizedAccountOrder(for accounts: [AccountInfo]) -> [Int] {
        let availableSlots = Set(accounts.map(\.slot))
        let savedSlots = accountOrder.filter(availableSlots.contains)
        return savedSlots + accounts.map(\.slot).filter { !savedSlots.contains($0) }
    }

    private func syncAccountOrder(with accounts: [AccountInfo]) {
        let normalizedOrder = normalizedAccountOrder(for: accounts)
        guard normalizedOrder != accountOrder else { return }
        accountOrder = normalizedOrder
        saveAccountOrder(normalizedOrder)
    }

    private func updateDrag(_ gesture: DragGesture.Value, slot: Int, accounts: [AccountInfo]) {
        guard draggedSlot == nil || draggedSlot == slot else { return }
        if draggedSlot != slot {
            accountOrder = normalizedAccountOrder(for: accounts)
            withAnimation(.easeOut(duration: 0.12)) {
                draggedSlot = slot
            }
        }
        NSCursor.closedHand.set()
        dragOffset = gesture.translation.height

        guard let currentFrame = accountFrames[slot] else { return }
        let currentMidY = currentFrame.midY + dragOffset
        var nearestSlot: Int? = nil
        var nearestDistance: CGFloat = .infinity
        var edge: VerticalEdge = .top

        for (otherSlot, frame) in accountFrames where otherSlot != slot {
            let distance = abs(frame.midY - currentMidY)
            if distance < nearestDistance {
                nearestDistance = distance
                nearestSlot = otherSlot
                edge = currentMidY < frame.midY ? .top : .bottom
            }
        }

        if dropTargetSlot != nearestSlot || dropTargetEdge != edge {
            dropTargetSlot = nearestSlot
            dropTargetEdge = edge
            if nearestSlot != nil {
                NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
            }
        }
    }

    private func finishDrag(slot: Int, accounts: [AccountInfo]) {
        guard draggedSlot == slot else { return }
        let targetSlot = dropTargetSlot
        let targetEdge = dropTargetEdge

        withAnimation(.easeOut(duration: 0.15)) {
            draggedSlot = nil
            dragOffset = 0
            dropTargetSlot = nil
            dropTargetEdge = nil
        }

        guard let targetSlot, targetSlot != slot else {
            NSCursor.openHand.set()
            return
        }

        var updatedOrder = normalizedAccountOrder(for: accounts)
        guard let sourceIndex = updatedOrder.firstIndex(of: slot),
              let destIndex = updatedOrder.firstIndex(of: targetSlot) else {
            NSCursor.openHand.set()
            return
        }

        updatedOrder.remove(at: sourceIndex)
        var insertionIndex = destIndex
        if targetEdge == .bottom {
            insertionIndex = min(insertionIndex + 1, updatedOrder.count)
        }
        if sourceIndex < destIndex && targetEdge == .top {
            insertionIndex = max(0, insertionIndex)
        }
        insertionIndex = min(max(0, insertionIndex), updatedOrder.count)
        updatedOrder.insert(slot, at: insertionIndex)

        accountOrder = updatedOrder
        saveAccountOrder(updatedOrder)
        NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
        NSCursor.openHand.set()
    }

    private func moveAccount(_ slot: Int, by offset: Int, accounts: [AccountInfo]) {
        var updatedOrder = normalizedAccountOrder(for: accounts)
        guard let currentIndex = updatedOrder.firstIndex(of: slot) else { return }
        let targetIndex = currentIndex + offset
        guard updatedOrder.indices.contains(targetIndex) else { return }
        updatedOrder.swapAt(currentIndex, targetIndex)
        withAnimation(.spring(response: 0.24, dampingFraction: 0.82)) {
            accountOrder = updatedOrder
        }
        saveAccountOrder(updatedOrder)
    }

    private func saveAccountOrder(_ order: [Int]) {
        UserDefaults.standard.set(order, forKey: "accountOrder")
    }
}

// MARK: - App delegate (status item, popover, timers, watcher)

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate, @preconcurrency UNUserNotificationCenterDelegate {
    static weak var shared: AppDelegate?

    private var statusItem: NSStatusItem?
    private let popover = NSPopover()
    private var eventMonitor: Any?
    private var keyEventMonitor: Any?
    private let state = AppState()
    private let antigravityController = AntigravityController()
    private var controller: StatusController?
    private var watcher: AuthFileWatcher?
    private var statusTimer: Timer?
    private var hiddenPollCount = 0
    private var stateSubscription: AnyCancellable?
    private var antigravitySubscription: AnyCancellable?
    private var languageSubscription: AnyCancellable?
    private var antigravityNotificationObserver: NSObjectProtocol?

    override init() {
        super.init()
        Self.shared = self
    }

    // MARK: Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let controller = StatusController(state: state)
        self.controller = controller

        Notifier.requestAuthorization()
        if Notifier.isAvailable {
            UNUserNotificationCenter.current().delegate = self
        }

        setupStatusItem()
        setupPopover(controller: controller)
        subscribeToState()

        controller.refreshStatus()
        controller.loadConfig()
        controller.refreshLoginItemState()
        antigravityController.refresh()

        antigravityNotificationObserver = NotificationCenter.default.addObserver(
            forName: .antigravityAccountsDidChange,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.antigravityController.refresh()
            }
        }

        startTimers()
        startAuthWatcher()
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let observer = antigravityNotificationObserver {
            NotificationCenter.default.removeObserver(observer)
        }
        stopEventMonitors()
        statusTimer?.invalidate()
        watcher?.stop()
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool {
        return true
    }

    // MARK: Status item

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.image = NSImage(systemSymbolName: "arrow.triangle.2.circlepath.circle.fill", accessibilityDescription: "KeySwitcher")
            button.imagePosition = .imageLeading
            button.target = self
            button.action = #selector(togglePopover(_:))
        }
        statusItem = item
        updateStatusItem()
    }

    private func subscribeToState() {
        stateSubscription = state.objectWillChange
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                // objectWillChange fires before mutation — hop one runloop turn.
                DispatchQueue.main.async { self?.updateStatusItem() }
            }
        antigravitySubscription = antigravityController.$status
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.updateStatusItem()
            }
        languageSubscription = LanguageManager.shared.$language
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.updateStatusItem()
            }
    }

    private func updateStatusItem() {
        guard let button = statusItem?.button else { return }
        button.image = NSImage(systemSymbolName: "arrow.triangle.2.circlepath.circle.fill", accessibilityDescription: "KeySwitcher")
        button.imagePosition = .imageLeading
        let font = NSFont.menuBarFont(ofSize: 0)

        let title = NSMutableAttributedString()

        func appendWindow(symbol: String, remaining: Int, allowed: Bool = true) {
            let color = usageColor(remaining: remaining, allowed: allowed)
            title.append(symbolText(symbol, color: color, font: font))
            title.append(NSAttributedString(
                string: " \(remaining)%",
                attributes: [.font: font, .foregroundColor: color]
            ))
        }

        let slots = state.traySlots

        for (index, slot) in slots.enumerated() {
            if index > 0 {
                title.append(NSAttributedString(
                    string: " | ",
                    attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]
                ))
            }

            switch slot {
            case .codex:
                if state.engineMissing || state.statusFailed {
                    title.append(NSAttributedString(
                        string: "KS ",
                        attributes: [.font: font, .foregroundColor: NSColor.labelColor]
                    ))
                    title.append(symbolText("exclamationmark.triangle", color: .systemOrange, font: font))
                } else if let status = state.status {
                    let activeAccount = status.accounts?.first(where: { $0.active == true })
                        ?? status.accounts?.first(where: { $0.slot == status.active_slot })
                    let accountName = shortAccountName(email: activeAccount?.email, slot: status.active_slot)

                    title.append(NSAttributedString(
                        string: accountName + " ",
                        attributes: [.font: font, .foregroundColor: NSColor.labelColor]
                    ))

                    let allowed = activeAccount?.usage?.allowed != false
                    let windows = menuUsageWindows(activeAccount?.usage)
                    if let short = windows.short?.used_percent {
                        let remaining = Int(remainingLimitPercent(fromUsed: short).rounded())
                        appendWindow(symbol: "clock", remaining: remaining, allowed: allowed)
                    }
                    if let weekly = windows.weekly?.used_percent {
                        let remaining = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                        if windows.short != nil {
                            title.append(NSAttributedString(
                                string: "   ",
                                attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]
                            ))
                        }
                        appendWindow(symbol: "calendar", remaining: remaining, allowed: allowed)
                    }
                    if windows.short == nil && windows.weekly == nil {
                        title.append(NSAttributedString(
                            string: "…",
                            attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]
                        ))
                    }
                } else {
                    title.append(NSAttributedString(
                        string: "KS",
                        attributes: [.font: font, .foregroundColor: NSColor.labelColor]
                    ))
                }

            case .agCliGemini, .agCliClaude:
                let hasIde = slots.contains { $0 == .agIdeGemini || $0 == .agIdeClaude }
                let prefix = hasIde ? "CLI: " : ""
                let isGemini = (slot == .agCliGemini)

                if let agStatus = antigravityController.status,
                   let profiles = agStatus.profiles,
                   !profiles.isEmpty {
                    let activeProfileID = agStatus.active?["cli"]
                    let profile = profiles.first(where: { $0.id == activeProfileID }) ?? profiles.first

                    if let profile = profile {
                        let agName = prefix + shortAccountName(email: profile.email, slot: nil)
                        title.append(NSAttributedString(
                            string: agName + " ",
                            attributes: [.font: font, .foregroundColor: NSColor.labelColor]
                        ))

                        let quota = profile.quota
                        var appendedAny = false

                        if isGemini {
                            let gUsage = (quota?.gemini?.ok == true && quota?.gemini?.stale != true) ? quota?.gemini : nil
                            let gWindows = menuUsageWindows(gUsage)
                            if gWindows.short != nil || gWindows.weekly != nil {
                                title.append(symbolText("sparkle", color: .secondaryLabelColor, font: font))
                                title.append(NSAttributedString(string: " ", attributes: [.font: font]))
                                let gAllowed = gUsage?.allowed != false
                                if let short = gWindows.short?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: short).rounded())
                                    appendWindow(symbol: "clock", remaining: remaining, allowed: gAllowed)
                                }
                                if let weekly = gWindows.weekly?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                                    if gWindows.short != nil {
                                        title.append(NSAttributedString(string: "  ", attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
                                    }
                                    appendWindow(symbol: "calendar", remaining: remaining, allowed: gAllowed)
                                }
                                appendedAny = true
                            }
                        } else {
                            let tUsage = (quota?.thirdParty?.ok == true && quota?.thirdParty?.stale != true) ? quota?.thirdParty : nil
                            let tWindows = menuUsageWindows(tUsage)
                            if tWindows.short != nil || tWindows.weekly != nil {
                                title.append(symbolText("bolt.fill", color: .secondaryLabelColor, font: font))
                                title.append(NSAttributedString(string: " ", attributes: [.font: font]))
                                let tAllowed = tUsage?.allowed != false
                                if let short = tWindows.short?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: short).rounded())
                                    appendWindow(symbol: "clock", remaining: remaining, allowed: tAllowed)
                                }
                                if let weekly = tWindows.weekly?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                                    if tWindows.short != nil {
                                        title.append(NSAttributedString(string: "  ", attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
                                    }
                                    appendWindow(symbol: "calendar", remaining: remaining, allowed: tAllowed)
                                }
                                appendedAny = true
                            }
                        }

                        if !appendedAny {
                            title.append(NSAttributedString(
                                string: "…",
                                attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]
                            ))
                        }
                    }
                } else {
                    title.append(NSAttributedString(
                        string: isGemini ? "AG(CLI ✨)" : "AG(CLI ⚡)",
                        attributes: [.font: font, .foregroundColor: NSColor.labelColor]
                    ))
                    title.append(symbolText("exclamationmark.triangle", color: .systemOrange, font: font))
                }

            case .agIdeGemini, .agIdeClaude:
                let hasCli = slots.contains { $0 == .agCliGemini || $0 == .agCliClaude }
                let prefix = hasCli ? "IDE: " : ""
                let isGemini = (slot == .agIdeGemini)

                if let agStatus = antigravityController.status,
                   let profiles = agStatus.profiles,
                   !profiles.isEmpty {
                    let activeProfileID = agStatus.active?["ide"]
                    let profile = profiles.first(where: { $0.id == activeProfileID }) ?? profiles.first

                    if let profile = profile {
                        let agName = prefix + shortAccountName(email: profile.email, slot: nil)
                        title.append(NSAttributedString(
                            string: agName + " ",
                            attributes: [.font: font, .foregroundColor: NSColor.labelColor]
                        ))

                        let quota = profile.quota
                        var appendedAny = false

                        if isGemini {
                            let gUsage = (quota?.gemini?.ok == true && quota?.gemini?.stale != true) ? quota?.gemini : nil
                            let gWindows = menuUsageWindows(gUsage)
                            if gWindows.short != nil || gWindows.weekly != nil {
                                title.append(symbolText("sparkle", color: .secondaryLabelColor, font: font))
                                title.append(NSAttributedString(string: " ", attributes: [.font: font]))
                                let gAllowed = gUsage?.allowed != false
                                if let short = gWindows.short?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: short).rounded())
                                    appendWindow(symbol: "clock", remaining: remaining, allowed: gAllowed)
                                }
                                if let weekly = gWindows.weekly?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                                    if gWindows.short != nil {
                                        title.append(NSAttributedString(string: "  ", attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
                                    }
                                    appendWindow(symbol: "calendar", remaining: remaining, allowed: gAllowed)
                                }
                                appendedAny = true
                            }
                        } else {
                            let tUsage = (quota?.thirdParty?.ok == true && quota?.thirdParty?.stale != true) ? quota?.thirdParty : nil
                            let tWindows = menuUsageWindows(tUsage)
                            if tWindows.short != nil || tWindows.weekly != nil {
                                title.append(symbolText("bolt.fill", color: .secondaryLabelColor, font: font))
                                title.append(NSAttributedString(string: " ", attributes: [.font: font]))
                                let tAllowed = tUsage?.allowed != false
                                if let short = tWindows.short?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: short).rounded())
                                    appendWindow(symbol: "clock", remaining: remaining, allowed: tAllowed)
                                }
                                if let weekly = tWindows.weekly?.used_percent {
                                    let remaining = Int(remainingLimitPercent(fromUsed: weekly).rounded())
                                    if tWindows.short != nil {
                                        title.append(NSAttributedString(string: "  ", attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
                                    }
                                    appendWindow(symbol: "calendar", remaining: remaining, allowed: tAllowed)
                                }
                                appendedAny = true
                            }
                        }

                        if !appendedAny {
                            title.append(NSAttributedString(
                                string: "…",
                                attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]
                            ))
                        }
                    }
                } else {
                    title.append(NSAttributedString(
                        string: isGemini ? "AG(IDE ✨)" : "AG(IDE ⚡)",
                        attributes: [.font: font, .foregroundColor: NSColor.labelColor]
                    ))
                    title.append(symbolText("exclamationmark.triangle", color: .systemOrange, font: font))
                }
            }
        }

        button.attributedTitle = title
    }

    /// Smooth green→red tint by how much of the window is left (100% → green, 0% → red).
    private func usageColor(remaining: Int, allowed: Bool = true) -> NSColor {
        if !allowed { return .systemRed }
        let r = CGFloat(max(0, min(100, remaining)))
        return NSColor(hue: r / 100.0 * 0.33, saturation: 0.9, brightness: 0.95, alpha: 1.0)
    }

    /// An SF Symbol, palette-tinted to `color`, as an inline attachment sized to the
    /// menu-bar font and vertically centered on its cap height.
    private func symbolText(_ name: String, color: NSColor, font: NSFont) -> NSAttributedString {
        let config = NSImage.SymbolConfiguration(pointSize: font.pointSize, weight: .regular)
            .applying(NSImage.SymbolConfiguration(paletteColors: [color]))
        guard let image = NSImage(systemSymbolName: name, accessibilityDescription: nil)?
            .withSymbolConfiguration(config) else {
            return NSAttributedString(string: "")
        }
        image.isTemplate = false
        let attachment = NSTextAttachment()
        attachment.image = image
        attachment.bounds = CGRect(x: 0, y: (font.capHeight - image.size.height) / 2.0,
                                   width: image.size.width, height: image.size.height)
        return NSAttributedString(attachment: attachment)
    }

    // MARK: Popover

    private func setupPopover(controller: StatusController) {
        let hosting = NSHostingController(rootView: PanelView(state: state, controller: controller, antigravityController: antigravityController).noFocusRing())
        hosting.sizingOptions = .preferredContentSize
        popover.contentViewController = hosting
        popover.behavior = .applicationDefined
        popover.animates = false
        popover.delegate = self
    }

    @objc private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            closePopover(sender)
        } else {
            showPopover(sender)
        }
    }

    private func showPopover(_ sender: Any?) {
        guard let button = statusItem?.button else { return }
        controller?.refreshIfNeeded(minInterval: 15, silent: true)
        antigravityController.refreshIfNeeded(minInterval: 15, silent: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        NSApp.activate(ignoringOtherApps: true)
        startEventMonitors()
    }

    func closePopover(_ sender: Any?) {
        stopEventMonitors()
        if popover.isShown {
            popover.performClose(sender)
        }
    }

    private func startEventMonitors() {
        stopEventMonitors()
        eventMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            Task { @MainActor in
                guard let self = self, self.popover.isShown else { return }
                let mouseLocation = NSEvent.mouseLocation
                if let button = self.statusItem?.button, let window = button.window {
                    let buttonRect = window.convertToScreen(button.bounds).insetBy(dx: -12, dy: -12)
                    let windowRect = window.frame.insetBy(dx: -12, dy: -12)
                    if buttonRect.contains(mouseLocation) || windowRect.contains(mouseLocation) {
                        return
                    }
                }
                if let popoverWindow = self.popover.contentViewController?.view.window {
                    if popoverWindow.frame.insetBy(dx: -8, dy: -8).contains(mouseLocation) {
                        return
                    }
                }
                self.closePopover(nil)
            }
        }
        keyEventMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 { // ESC key
                Task { @MainActor in
                    self?.closePopover(nil)
                }
                return nil
            }
            return event
        }
    }

    private func stopEventMonitors() {
        if let monitor = eventMonitor {
            NSEvent.removeMonitor(monitor)
            eventMonitor = nil
        }
        if let monitor = keyEventMonitor {
            NSEvent.removeMonitor(monitor)
            keyEventMonitor = nil
        }
    }

    func popoverDidClose(_ notification: Notification) {
        stopEventMonitors()
    }

    // MARK: Timers (serial per-kind: controller skips overlapping ticks)

    private func startTimers() {
        statusTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                // Poll every 30s while the popover is open, every 60s otherwise.
                if self.popover.isShown || self.hiddenPollCount % 2 == 1 {
                    self.hiddenPollCount = 0
                    self.controller?.refreshStatus(silent: true)
                    self.antigravityController.refresh(silent: true)
                } else {
                    self.hiddenPollCount += 1
                }
            }
        }
    }

    // MARK: auth.json watcher

    private func startAuthWatcher() {
        let authPath = NSHomeDirectory() + "/.codex/auth.json"
        let fileWatcher = AuthFileWatcher(path: authPath)
        fileWatcher.onChange = { [weak self] in
            self?.controller?.refreshStatus(silent: true)
        }
        fileWatcher.start()
        watcher = fileWatcher
    }

    // MARK: UNUserNotificationCenterDelegate

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}

// MARK: - Entry point

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let appDelegate = AppDelegate()
    app.delegate = appDelegate
    app.setActivationPolicy(.accessory)
    app.run()
}
