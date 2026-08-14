import SwiftUI

struct AntigravityProfile: Codable, Identifiable, Sendable {
    let id: String
    let email: String
    let targets: [String]
    let plan: String?
    let quota: AntigravityQuota?
}

struct AntigravityQuota: Codable, Sendable {
    let gemini: UsageInfo?
    let thirdParty: UsageInfo?

    enum CodingKeys: String, CodingKey {
        case gemini
        case thirdParty = "third_party"
    }
}

struct AntigravityTargetState: Codable, Sendable {
    let available: Bool
    let installed: Bool
}

struct AntigravityResponse: Codable, Sendable {
    let ok: Bool?
    let profiles: [AntigravityProfile]?
    let order: [String: [String]]?
    let active: [String: String]?
    let autoswitch: [String: Bool]?
    let targets: [String: AntigravityTargetState]?
    let pending: Bool?
    let pendingTargets: [String]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, profiles, order, active, autoswitch, targets, pending, error
        case pendingTargets = "pending_targets"
    }
}

struct AntigravityAutoCheckResponse: Codable, Sendable {
    let ok: Bool?
    let switchedTargets: [String]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case switchedTargets = "switched_targets"
    }
}

extension Notification.Name {
    static let antigravityAccountsDidChange = Notification.Name(
        "com.eugene.keyswitcher.antigravityAccountsDidChange"
    )
}

private struct AntigravityProfileFramePreferenceKey: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]

    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

@MainActor
final class AntigravityController: ObservableObject {
    @Published var status: AntigravityResponse?
    @Published var isLoading = false
    @Published var message: String?
    @Published var isError = false
    @Published var addingTarget: String?
    @Published var order = ["cli": [String](), "ide": [String]()]

    private let engine = EngineClient()
    private var loginGeneration = 0
    private var lastRefreshTime: Date?

    func refresh(silent: Bool = false) {
        guard !isLoading else { return }
        loadStatus(message: nil, silent: silent)
    }

    func refreshIfNeeded(minInterval: TimeInterval = 15, silent: Bool = true) {
        if status != nil, let last = lastRefreshTime, Date().timeIntervalSince(last) < minInterval {
            return
        }
        refresh(silent: silent)
    }

    func beginLogin(target: String = "cli") {
        guard !isLoading, addingTarget == nil else { return }
        loginGeneration += 1
        let generation = loginGeneration
        addingTarget = target
        isLoading = true
        message = nil
        engine.run(["antigravity", "begin-login", target], as: AntigravityResponse.self) { [weak self] result in
            guard let self, generation == self.loginGeneration else { return }
            self.isLoading = false
            switch result {
            case .success(let response) where response.ok == true:
                self.pollLogin(target: target, generation: generation)
            case .success(let response):
                self.addingTarget = nil
                self.finishWithError(response.error ?? "Не удалось начать вход")
            case .failure(let error):
                self.addingTarget = nil
                self.finishWithError(error.displayText)
            }
        }
    }

    func cancelLogin() {
        guard let target = addingTarget else { return }
        loginGeneration += 1
        isLoading = true
        engine.run(["antigravity", "cancel-login", target], as: AntigravityResponse.self) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let response) where response.ok == true:
                self.addingTarget = nil
                self.loadStatus(message: "Вход отменён")
            case .success(let response):
                self.finishWithError(response.error ?? "Не удалось отменить вход")
            case .failure(let error):
                self.finishWithError(error.displayText)
            }
        }
    }

    func switchTo(profileID: String, target: String = "all") {
        let msg = target == "all" ? "Аккаунт переключён везде" : "Аккаунт переключён"
        run(["antigravity", "switch", profileID, target], success: msg)
    }

    func remove(profileID: String, target: String = "all") {
        run(["antigravity", "remove", profileID, target], success: "Аккаунт удалён из свитчера")
    }

    func reorder(profileIDs: [String], target: String = "all") {
        let previousCli = order["cli"] ?? []
        let previousIde = order["ide"] ?? []
        order["cli"] = profileIDs
        order["ide"] = profileIDs
        engine.run(
            ["antigravity", "reorder", target] + profileIDs,
            as: AntigravityResponse.self
        ) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let response) where response.ok == true:
                if let updated = response.order {
                    self.order.merge(updated) { _, new in new }
                }
            case .success(let response):
                self.order["cli"] = previousCli
                self.order["ide"] = previousIde
                self.finishWithError(response.error ?? "Не удалось сохранить порядок")
            case .failure(let error):
                self.order["cli"] = previousCli
                self.order["ide"] = previousIde
                self.finishWithError(error.displayText)
            }
        }
    }

    private func run(_ arguments: [String], success: String) {
        guard !isLoading else { return }
        isLoading = true
        message = nil
        engine.run(arguments, as: AntigravityResponse.self) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let response) where response.ok == true:
                self.loadStatus(message: success)
            case .success(let response):
                self.finishWithError(response.error ?? "Операция не выполнена")
            case .failure(let error):
                self.finishWithError(error.displayText)
            }
        }
    }

    private func loadStatus(message successMessage: String?, silent: Bool = false) {
        if !silent {
            isLoading = true
        }
        engine.run(["antigravity", "status"], as: AntigravityResponse.self) { [weak self] result in
            guard let self else { return }
            self.lastRefreshTime = Date()
            if !silent {
                self.isLoading = false
            }
            switch result {
            case .success(let response) where response.ok == true:
                self.status = response
                if let updated = response.order {
                    self.order.merge(updated) { _, new in new }
                }
                self.message = successMessage
                self.isError = false
                if self.addingTarget == nil, let target = response.pendingTargets?.first {
                    self.loginGeneration += 1
                    let generation = self.loginGeneration
                    self.addingTarget = target
                    self.pollLogin(target: target, generation: generation)
                }
            case .success(let response):
                self.finishWithError(response.error ?? "Не удалось загрузить аккаунты")
            case .failure(let error):
                self.finishWithError(error.displayText)
            }
        }
    }

    private func finishWithError(_ text: String) {
        isLoading = false
        message = text
        isError = true
    }

    private func pollLogin(target: String, generation: Int) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self, generation == self.loginGeneration, self.addingTarget == target else { return }
            self.engine.run(
                ["antigravity", "finish-login", target],
                as: AntigravityResponse.self
            ) { [weak self] result in
                guard let self, generation == self.loginGeneration else { return }
                switch result {
                case .success(let response) where response.ok == true && response.pending == true:
                    self.pollLogin(target: target, generation: generation)
                case .success(let response) where response.ok == true:
                    self.addingTarget = nil
                    self.loadStatus(message: "Новый аккаунт сохранён")
                case .success(let response):
                    self.addingTarget = nil
                    self.finishWithError(response.error ?? "Не удалось завершить вход")
                case .failure(let error):
                    self.addingTarget = nil
                    self.finishWithError(error.displayText)
                }
            }
        }
    }
}

struct AntigravityPanelView: View {
    @ObservedObject var controller: AntigravityController
    @State private var isAddHovered = false
    @State private var isRefreshHovered = false
    @State private var removal: AntigravityRemoval?
    @State private var draggedProfileID: String?
    @State private var dragOffset: CGFloat = 0
    @State private var dropTargetProfileID: String?
    @State private var dropTargetEdge: VerticalEdge?
    @State private var profileFrames: [String: CGRect] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()
            loginProgress
            content
            if let message = controller.message {
                Text(message)
                    .font(.caption2)
                    .foregroundColor(controller.isError ? .red : .secondary)
                    .lineLimit(2)
            }
        }
        .onAppear { controller.refreshIfNeeded(minInterval: 15, silent: true) }
        .onReceive(NotificationCenter.default.publisher(for: .antigravityAccountsDidChange)) { _ in
            controller.refreshIfNeeded(minInterval: 2, silent: true)
        }
        .alert("Удалить аккаунт из свитчера?", isPresented: Binding(
            get: { removal != nil },
            set: { if !$0 { removal = nil } }
        )) {
            if let removal {
                Button("Удалить", role: .destructive) {
                    controller.remove(profileID: removal.profileID, target: "all")
                    self.removal = nil
                }
                Button("Отмена", role: .cancel) { self.removal = nil }
            }
        } message: {
            Text("Текущая сессия в Antigravity останется активной. Удалится только сохранённая копия из KeySwitcher.")
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Antigravity KeySwitcher")
                .font(.headline)
            Spacer()
            Button {
                controller.beginLogin(target: "cli")
            } label: {
                HoverIcon(systemName: "plus", isHovered: isAddHovered)
            }
            .buttonStyle(.borderless)
            .disabled(controller.isLoading || controller.addingTarget != nil)
            .accessibilityLabel("Добавить аккаунт")
            .onHover { isAddHovered = $0 }
            .nonFocusable()
            Button {
                controller.refresh(silent: false)
            } label: {
                HoverIcon(
                    systemName: "arrow.clockwise",
                    showsProgress: controller.isLoading,
                    isHovered: isRefreshHovered
                )
            }
            .buttonStyle(.borderless)
            .disabled(controller.isLoading || controller.addingTarget != nil)
            .accessibilityLabel("Обновить")
            .onHover { isRefreshHovered = $0 }
            .nonFocusable()
        }
    }

    @ViewBuilder
    private var loginProgress: some View {
        if controller.addingTarget != nil {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Вход в Google Antigravity…")
                    .font(.callout)
                    .foregroundColor(.secondary)
                Spacer()
                Button("Отмена") {
                    controller.cancelLogin()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(controller.isLoading)
                .nonFocusable()
            }
            .padding(.vertical, 4)
        }
    }

    @ViewBuilder
    private var content: some View {
        if let profiles = controller.status?.profiles, !profiles.isEmpty {
            ScrollView {
                profilesList(profiles)
            }
            .frame(maxHeight: 320)
        } else if controller.isLoading {
            Text("Загрузка…")
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 12)
        } else {
            VStack(spacing: 4) {
                Text("Сохранённых аккаунтов пока нет")
                    .font(.callout)
                    .foregroundColor(.secondary)
                Text("Добавьте аккаунт через +")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 12)
        }
    }

    private func profilesList(_ profiles: [AntigravityProfile]) -> some View {
        let targetProfiles = orderedProfiles(profiles)
        return VStack(alignment: .leading, spacing: 6) {
            ForEach(targetProfiles) { profile in
                let isCliActive = controller.status?.active?["cli"] == profile.id
                let isIdeActive = controller.status?.active?["ide"] == profile.id
                AntigravityAccountCard(
                    profile: profile,
                    isCliActive: isCliActive,
                    isIdeActive: isIdeActive,
                    isLoading: controller.isLoading || controller.addingTarget != nil,
                    onSwitchAll: {
                        controller.switchTo(profileID: profile.id, target: "all")
                    },
                    onSwitchCli: {
                        controller.switchTo(profileID: profile.id, target: "cli")
                    },
                    onSwitchIde: {
                        controller.switchTo(profileID: profile.id, target: "ide")
                    },
                    onRemove: {
                        removal = AntigravityRemoval(profileID: profile.id)
                    },
                    onDragChanged: {
                        updateDrag($0, profileID: profile.id, profiles: targetProfiles)
                    },
                    onDragEnded: {
                        finishDrag(profileID: profile.id, profiles: targetProfiles)
                    }
                )
                .background {
                    GeometryReader { geometry in
                        Color.clear.preference(
                            key: AntigravityProfileFramePreferenceKey.self,
                            value: [
                                profile.id: geometry.frame(in: .named("antigravityAccountList"))
                            ]
                        )
                    }
                }
                .offset(y: draggedProfileID == profile.id ? dragOffset : 0)
                .scaleEffect(draggedProfileID == profile.id ? 1.015 : 1)
                .shadow(
                    color: .black.opacity(draggedProfileID == profile.id ? 0.24 : 0),
                    radius: draggedProfileID == profile.id ? 12 : 0,
                    y: draggedProfileID == profile.id ? 6 : 0
                )
                .zIndex(draggedProfileID == profile.id ? 10 : 0)
                .overlay(alignment: dropIndicatorAlignment) {
                    dropIndicator(for: profile.id)
                }
                .accessibilityAction(named: Text("Переместить выше")) {
                    moveProfile(profile.id, by: -1, profiles: targetProfiles)
                }
                .accessibilityAction(named: Text("Переместить ниже")) {
                    moveProfile(profile.id, by: 1, profiles: targetProfiles)
                }
            }
        }
        .coordinateSpace(name: "antigravityAccountList")
        .onPreferenceChange(AntigravityProfileFramePreferenceKey.self) { frames in
            if draggedProfileID == nil {
                profileFrames = frames
            }
        }
    }

    private func orderedProfiles(_ profiles: [AntigravityProfile]) -> [AntigravityProfile] {
        let byID = Dictionary(uniqueKeysWithValues: profiles.map { ($0.id, $0) })
        let saved = (controller.order["cli"] ?? controller.order["ide"] ?? []).filter { byID[$0] != nil }
        let normalized = saved + profiles.map(\.id).filter { !saved.contains($0) }
        return normalized.compactMap { byID[$0] }
    }

    private var dropIndicatorAlignment: Alignment {
        dropTargetEdge == .bottom ? .bottom : .top
    }

    @ViewBuilder
    private func dropIndicator(for profileID: String) -> some View {
        if dropTargetProfileID == profileID && draggedProfileID != profileID {
            Capsule()
                .fill(Color.accentColor)
                .frame(height: 3)
                .padding(.horizontal, 8)
                .offset(y: dropTargetEdge == .bottom ? 5 : -5)
        }
    }

    private func updateDrag(
        _ gesture: DragGesture.Value,
        profileID: String,
        profiles: [AntigravityProfile]
    ) {
        if draggedProfileID != profileID {
            withAnimation(.easeOut(duration: 0.12)) {
                draggedProfileID = profileID
            }
        }
        NSCursor.closedHand.set()
        dragOffset = gesture.translation.height

        let targetID = profileFrames.min {
            abs($0.value.midY - gesture.location.y) < abs($1.value.midY - gesture.location.y)
        }?.key
        guard targetID != dropTargetProfileID else { return }
        dropTargetProfileID = targetID

        if let targetID,
           targetID != profileID,
           let fromIndex = profiles.firstIndex(where: { $0.id == profileID }),
           let targetIndex = profiles.firstIndex(where: { $0.id == targetID }) {
            dropTargetEdge = targetIndex > fromIndex ? .bottom : .top
            NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
        } else {
            dropTargetEdge = nil
        }
    }

    private func finishDrag(
        profileID: String,
        profiles: [AntigravityProfile]
    ) {
        var order = profiles.map(\.id)
        if let targetID = dropTargetProfileID,
           targetID != profileID,
           let fromIndex = order.firstIndex(of: profileID),
           let targetIndex = order.firstIndex(of: targetID) {
            order.move(
                fromOffsets: IndexSet(integer: fromIndex),
                toOffset: targetIndex > fromIndex ? targetIndex + 1 : targetIndex
            )
            controller.reorder(profileIDs: order, target: "all")
            NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
        }

        withAnimation(.spring(response: 0.28, dampingFraction: 0.82)) {
            resetDrag()
        }
        NSCursor.openHand.set()
    }

    private func moveProfile(
        _ profileID: String,
        by offset: Int,
        profiles: [AntigravityProfile]
    ) {
        var order = profiles.map(\.id)
        guard let currentIndex = order.firstIndex(of: profileID) else { return }
        let targetIndex = currentIndex + offset
        guard order.indices.contains(targetIndex) else { return }
        order.swapAt(currentIndex, targetIndex)
        withAnimation(.spring(response: 0.24, dampingFraction: 0.82)) {
            controller.reorder(profileIDs: order, target: "all")
        }
    }

    private func resetDrag() {
        draggedProfileID = nil
        dragOffset = 0
        dropTargetProfileID = nil
        dropTargetEdge = nil
        profileFrames = [:]
    }
}

private struct AntigravityRemoval {
    let profileID: String
}

private struct AntigravityAccountCard: View {
    let profile: AntigravityProfile
    let isCliActive: Bool
    let isIdeActive: Bool
    let isLoading: Bool
    let onSwitchAll: () -> Void
    let onSwitchCli: () -> Void
    let onSwitchIde: () -> Void
    let onRemove: () -> Void
    let onDragChanged: (DragGesture.Value) -> Void
    let onDragEnded: () -> Void
    @State private var isDragHandleHovered = false
    @State private var isActionsHovered = false
    @State private var isEmailRevealed = false
    @State private var isEmailHovered = false

    private var isFullyActive: Bool { isCliActive && isIdeActive }
    private var isPartiallyActive: Bool { isCliActive || isIdeActive }

    private var activeBadgeText: String? {
        if isCliActive && isIdeActive { return "Активен" }
        if isCliActive { return "Активен (CLI)" }
        if isIdeActive { return "Активен (IDE)" }
        return nil
    }

    private var displayedAccountName: String {
        if isEmailRevealed {
            return profile.email
        }
        return shortAccountName(email: profile.email, slot: nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "line.3.horizontal")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(
                        isDragHandleHovered ? .accentColor : Color.primary.opacity(0.8)
                    )
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
                        DragGesture(
                            minimumDistance: 2,
                            coordinateSpace: .named("antigravityAccountList")
                        )
                        .onChanged(onDragChanged)
                        .onEnded { _ in onDragEnded() }
                    )
                    .allowsHitTesting(!isLoading)
                    .accessibilityLabel("Изменить порядок аккаунта")
                    .accessibilityHint("Перетащите вверх или вниз")
                if isPartiallyActive {
                    Circle()
                        .fill(Color.green)
                        .frame(width: 10, height: 10)
                }
                Text(displayedAccountName)
                    .font(.callout)
                    .fontWeight(isPartiallyActive ? .bold : .regular)
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
                    .help(isEmailRevealed ? "Нажмите, чтобы скрыть полный email" : "Нажмите, чтобы показать полный email")
                if let plan = profile.plan, !plan.isEmpty {
                    Text(plan.capitalized)
                        .font(.caption2)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(Color.accentColor.opacity(0.18)))
                        .foregroundColor(.accentColor)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                }
                Spacer(minLength: 4)
                if let badge = activeBadgeText {
                    Text(badge)
                        .font(.caption)
                        .foregroundColor(.green)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                }
                Menu {
                    if !isFullyActive {
                        Button {
                            onSwitchAll()
                        } label: {
                            Label("Переключить везде (CLI + IDE)", systemImage: "arrow.right.circle")
                        }
                        Divider()
                    }
                    if !isCliActive {
                        Button {
                            onSwitchCli()
                        } label: {
                            Label("Переключить только CLI", systemImage: "terminal")
                        }
                    }
                    if !isIdeActive {
                        Button {
                            onSwitchIde()
                        } label: {
                            Label("Переключить только IDE", systemImage: "chevron.left.forwardslash.chevron.right")
                        }
                    }
                    Divider()
                    Button("Удалить из свитчера", role: .destructive, action: onRemove)
                } label: {
                    HoverIcon(systemName: "ellipsis", isHovered: isActionsHovered)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .controlSize(.small)
                .disabled(isLoading)
                .accessibilityLabel("Действия аккаунта")
                .onHover { isActionsHovered = $0 }
                .nonFocusable()
            }
            quotaSection
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(
                    isPartiallyActive ? Color.green.opacity(0.6) : Color.primary.opacity(0.12),
                    lineWidth: 1
                )
        )
    }

    @ViewBuilder
    private var quotaSection: some View {
        if let quota = profile.quota {
            HStack(alignment: .top, spacing: 10) {
                quotaGroup("Gemini", usage: quota.gemini)
                    .frame(maxWidth: .infinity, alignment: .leading)

                Divider()
                    .padding(.vertical, 1)

                quotaGroup("Claude / GPT", usage: quota.thirdParty)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.top, 2)
        }
    }

    @ViewBuilder
    private func quotaGroup(_ title: String, usage: UsageInfo?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.caption2)
                .fontWeight(.semibold)
                .foregroundColor(.secondary)
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
            if let usage, usage.ok == true, usage.stale != true {
                let windows = menuUsageWindows(usage)
                if let short = windows.short {
                    UsageBarRow(label: "5 ч", window: short, stale: usage.stale ?? false)
                }
                if let weekly = windows.weekly {
                    UsageBarRow(label: "Неделя", window: weekly, stale: usage.stale ?? false)
                }
                if windows.short == nil && windows.weekly == nil {
                    noQuotaText
                }
            } else {
                noQuotaText
            }
        }
    }

    private var noQuotaText: some View {
        Text("нет данных")
            .font(.caption)
            .foregroundColor(.secondary)
    }
}
