import Foundation
import SwiftUI

public enum AppLanguage: String, CaseIterable, Identifiable {
    case system = "system"
    case en = "en"
    case ru = "ru"

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .system:
            return LanguageManager.shared.isRussian ? "Системный" : "System"
        case .en:
            return "English"
        case .ru:
            return "Русский"
        }
    }
}

public enum ResolvedLanguage {
    case en
    case ru
}

public final class LanguageManager: ObservableObject {
    public static let shared = LanguageManager()

    private let userDefaultsKey = "selectedAppLanguage"
    private let lock = NSLock()

    @Published public var language: AppLanguage {
        didSet {
            UserDefaults.standard.set(language.rawValue, forKey: userDefaultsKey)
            updateResolvedLanguage()
        }
    }

    @Published public private(set) var resolved: ResolvedLanguage = .en

    private init() {
        let saved = UserDefaults.standard.string(forKey: userDefaultsKey) ?? AppLanguage.system.rawValue
        let initial = AppLanguage(rawValue: saved) ?? .system
        self.language = initial
        self.resolved = Self.computeResolved(for: initial)
    }

    public func updateResolvedLanguage() {
        let newResolved = Self.computeResolved(for: language)
        lock.lock()
        resolved = newResolved
        lock.unlock()
    }

    private static func computeResolved(for language: AppLanguage) -> ResolvedLanguage {
        switch language {
        case .en:
            return .en
        case .ru:
            return .ru
        case .system:
            let preferred = Locale.preferredLanguages.first?.lowercased() ?? "en"
            return preferred.starts(with: "ru") ? .ru : .en
        }
    }

    public var isRussian: Bool {
        lock.lock()
        defer { lock.unlock() }
        return resolved == .ru
    }
}

public enum L10n {
    public static var isRu: Bool {
        LanguageManager.shared.isRussian
    }

    // Headers & Labels
    public static var codexHeader: String { "Codex KeySwitcher" }
    public static var antigravityHeader: String { "Antigravity KeySwitcher" }
    public static var addAccount: String { isRu ? "Добавить аккаунт" : "Add account" }
    public static var refresh: String { isRu ? "Обновить" : "Refresh" }
    public static var settings: String { isRu ? "Настройки приложения" : "App settings" }
    public static var launchAtLogin: String { isRu ? "Запускать при входе" : "Launch at login" }
    public static var languageMenu: String { isRu ? "Язык" : "Language" }
    public static var trayDisplayMenu: String { isRu ? "Показывать в строке меню" : "Show in menu bar" }
    public static var trayDisplayBoth: String { "Codex + Antigravity" }
    public static var trayDisplayCodex: String { "Codex" }
    public static var trayDisplayAntigravity: String { "Antigravity" }
    public static var quit: String { isRu ? "Выйти" : "Quit" }

    // Account Card Status
    public static var active: String { isRu ? "Активен" : "Active" }
    public static var activeCli: String { isRu ? "Активен (Agent / CLI)" : "Active (Agent / CLI)" }
    public static var activeIde: String { isRu ? "Активен (IDE)" : "Active (IDE)" }
    public static var openingLogin: String { isRu ? "Открываю вход…" : "Opening login…" }
    public static var logIn: String { isRu ? "Войти" : "Log in" }
    public static var accountActions: String { isRu ? "Действия аккаунта" : "Account actions" }
    public static var switchAction: String { isRu ? "Переключить" : "Switch" }
    public static var switchEverywhere: String { isRu ? "Переключить везде (CLI + IDE)" : "Switch everywhere (CLI + IDE)" }
    public static var switchCliOnly: String { isRu ? "Переключить только Agent App / CLI" : "Switch Agent App / CLI only" }
    public static var switchIdeOnly: String { isRu ? "Переключить только IDE" : "Switch IDE only" }
    public static var removeFromSwitcher: String { isRu ? "Удалить из свитчера" : "Remove from switcher" }
    public static var deleteAccount: String { isRu ? "Удалить аккаунт" : "Delete account" }
    public static var delete: String { isRu ? "Удалить" : "Delete" }
    public static var cancel: String { isRu ? "Отмена" : "Cancel" }

    // Usage & Quotas
    public static var fiveHours: String { isRu ? "5 ч" : "5h" }
    public static var week: String { isRu ? "Неделя" : "Week" }
    public static var noData: String { isRu ? "нет данных" : "no data" }
    public static var sessionExpired: String { isRu ? "сессия завершена — нужен повторный вход" : "session expired — login required" }

    // Placeholders & Loading
    public static var engineMissingTitle: String { isRu ? "Движок не найден" : "Engine not found" }
    public static var engineMissingSubtitle: String { isRu ? "Ожидается keyswitcher.py в Resources приложения" : "Expected keyswitcher.py in app Resources" }
    public static var loading: String { isRu ? "Загрузка…" : "Loading…" }
    public static var noSavedAccounts: String { isRu ? "Сохранённых аккаунтов пока нет" : "No saved accounts yet" }
    public static var addAccountViaPlus: String { isRu ? "Добавьте аккаунт через +" : "Add an account using +" }
    public static var signingInAntigravity: String { isRu ? "Вход в Google Antigravity…" : "Signing in to Google Antigravity…" }
    public static var signingInBrowser: String { isRu ? "Авторизация в браузере..." : "Signing in via browser..." }

    // Reorder & Accessibility
    public static var moveUp: String { isRu ? "Переместить выше" : "Move up" }
    public static var moveDown: String { isRu ? "Переместить ниже" : "Move down" }
    public static var reorderAccount: String { isRu ? "Изменить порядок аккаунта" : "Reorder account" }
    public static var dragHint: String { isRu ? "Перетащите вверх или вниз" : "Drag up or down" }
    public static var showFullEmail: String { isRu ? "Нажмите, чтобы показать полный email" : "Click to reveal full email" }
    public static var hideFullEmail: String { isRu ? "Нажмите, чтобы скрыть полный email" : "Click to hide full email" }

    // Time & Interval Formatting
    public static func resetInterval(secondsFromNow seconds: Int) -> String {
        if seconds <= 0 {
            return isRu ? "сброс скоро" : "resets soon"
        }
        let days = seconds / 86400
        let hours = (seconds % 86400) / 3600
        let minutes = (seconds % 3600) / 60
        var parts: [String] = []
        if isRu {
            if days > 0 { parts.append("\(days) д") }
            if hours > 0 { parts.append("\(hours) ч") }
            if minutes > 0 { parts.append("\(minutes) мин") }
            if parts.isEmpty { return "сброс меньше чем через минуту" }
            return "сброс через " + parts.joined(separator: " ")
        } else {
            if days > 0 { parts.append("\(days)d") }
            if hours > 0 { parts.append("\(hours)h") }
            if minutes > 0 { parts.append("\(minutes)m") }
            if parts.isEmpty { return "resets in less than a minute" }
            return "resets in " + parts.joined(separator: " ")
        }
    }

    public static func resetClock(timestamp: Int) -> String {
        let formatter = DateFormatter()
        formatter.locale = isRu ? Locale(identifier: "ru_RU") : Locale(identifier: "en_US")
        let date = Date(timeIntervalSince1970: TimeInterval(timestamp))
        formatter.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm" : "EEE HH:mm"
        return formatter.string(from: date)
    }

    // Alerts
    public static var deleteAccountTitle: String { isRu ? "Удалить аккаунт?" : "Delete account?" }
    public static var removeAccountTitle: String { isRu ? "Удалить аккаунт из свитчера?" : "Remove account from switcher?" }

    public static func deleteCodexActiveMessage(email: String) -> String {
        isRu
            ? "Это активный аккаунт \(email). Удаление разлогинит Codex (будет удалён ~/.codex/auth.json). Продолжить?"
            : "This is the active account \(email). Removing it will log out Codex (deleting ~/.codex/auth.json). Continue?"
    }

    public static func deleteCodexSlotMessage(email: String) -> String {
        isRu
            ? "Удалить аккаунт \(email) из KeySwitcher?"
            : "Remove account \(email) from KeySwitcher?"
    }

    public static var removeAntigravityMessage: String {
        isRu
            ? "Текущая сессия в Antigravity останется активной. Удалится только сохранённая копия из KeySwitcher."
            : "Active Antigravity session will stay active. Only saved snapshot in KeySwitcher will be removed."
    }

    // Notifications & Errors
    public static func switchedTo(email: String, withoutRestart: Bool = false) -> String {
        let suffix = withoutRestart ? (isRu ? " без рестарта" : " without restart") : ""
        return isRu ? "Переключено на \(email)\(suffix)" : "Switched to \(email)\(suffix)"
    }

    public static func addedAccount(email: String) -> String {
        isRu ? "Добавлен новый аккаунт: \(email)" : "Added new account: \(email)"
    }

    public static func deletedAccount(email: String) -> String {
        isRu ? "Аккаунт \(email) успешно удалён" : "Account \(email) deleted successfully"
    }

    public static var switchEverywhereSuccess: String { isRu ? "Аккаунт переключён везде" : "Account switched everywhere" }
    public static var switchSuccess: String { isRu ? "Аккаунт переключён" : "Account switched" }
    public static var removeSuccess: String { isRu ? "Аккаунт удалён из свитчера" : "Account removed from switcher" }
    public static var loginCanceled: String { isRu ? "Вход отменён" : "Login canceled" }
    public static var newAccountSaved: String { isRu ? "Новый аккаунт сохранён" : "New account saved" }
    public static var authUpdated: String { isRu ? "Авторизация обновлена" : "Authorization updated" }

    public static var failedToSwitch: String { isRu ? "Не удалось переключить аккаунт" : "Failed to switch account" }
    public static var failedToUpdateAuth: String { isRu ? "Не удалось обновить авторизацию" : "Failed to update authorization" }
    public static var failedToAddAccount: String { isRu ? "Не удалось добавить аккаунт" : "Failed to add account" }
    public static var failedToDeleteAccount: String { isRu ? "Не удалось удалить аккаунт" : "Failed to delete account" }
    public static var failedToCancelLogin: String { isRu ? "Не удалось отменить вход" : "Failed to cancel login" }
    public static var failedToSaveOrder: String { isRu ? "Не удалось сохранить порядок" : "Failed to save order" }
    public static var operationFailed: String { isRu ? "Операция не выполнена" : "Operation failed" }
    public static var failedToLoadAccounts: String { isRu ? "Не удалось загрузить аккаунты" : "Failed to load accounts" }
    public static var failedToCompleteLogin: String { isRu ? "Не удалось завершить вход" : "Failed to complete login" }
    public static var loginTimedOut: String { isRu ? "Вход не завершён вовремя — попробуйте ещё раз" : "Login timed out — please try again" }
}
