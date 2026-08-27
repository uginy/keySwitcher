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
                self.finishWithError(response.error ?? L10n.failedToCancelLogin)
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
                self.loadStatus(message: L10n.loginCanceled)
            case .success(let response):
                self.finishWithError(response.error ?? L10n.failedToCancelLogin)
            case .failure(let error):
                self.finishWithError(error.displayText)
            }
        }
    }

    func switchTo(profileID: String, target: String = "all") {
        let msg = target == "all" ? L10n.switchEverywhereSuccess : L10n.switchSuccess
        run(["antigravity", "switch", profileID, target], success: msg)
    }

    func remove(profileID: String, target: String = "all") {
        run(["antigravity", "remove", profileID, target], success: L10n.removeSuccess)
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
                self.finishWithError(response.error ?? L10n.failedToSaveOrder)
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
                self.finishWithError(response.error ?? L10n.operationFailed)
            case .failure(let error):
                self.finishWithError(error.displayText)
            }
        }
    }

    private func loadStatus(message successMessage: String?, silent: Bool = false) {
        if !silent {
            isLoading = true
        }
        let args = silent ? ["antigravity", "status"] : ["antigravity", "status", "--sync-cli"]
        engine.run(args, as: AntigravityResponse.self) { [weak self] result in
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
                self.finishWithError(response.error ?? L10n.failedToLoadAccounts)
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
                    self.loadStatus(message: L10n.newAccountSaved)
                case .success(let response):
                    self.addingTarget = nil
                    self.finishWithError(response.error ?? L10n.failedToCompleteLogin)
                case .failure(let error):
                    self.addingTarget = nil
                    self.finishWithError(error.displayText)
                }
            }
        }
    }
}

// MARK: - Views

private struct AntigravityRemoval {
    let profileID: String
}

private struct AntigravityFramePreferenceKey: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]

    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

struct AntigravityPanelView: View {
    @ObservedObject var controller: AntigravityController
    @ObservedObject private var languageManager = LanguageManager.shared
    @State private var isAddHovered = false
    @State private var isRefreshHovered = false
    @State private var removal: AntigravityRemoval? = nil
    @State private var draggedProfileID: String? = nil
    @State private var dragOffset: CGFloat = 0
    @State private var dropTargetID: String? = nil
    @State private var dropTargetEdge: VerticalEdge? = nil
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
        .alert(L10n.removeAccountTitle, isPresented: Binding(
            get: { removal != nil },
            set: { if !$0 { removal = nil } }
        )) {
            if let removal {
                Button(L10n.delete, role: .destructive) {
                    controller.remove(profileID: removal.profileID, target: "all")
                    self.removal = nil
                }
                Button(L10n.cancel, role: .cancel) { self.removal = nil }
            }
        } message: {
            Text(L10n.removeAntigravityMessage)
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text(L10n.antigravityHeader)
                .font(.headline)
            Spacer()
            Button {
                controller.beginLogin(target: "cli")
            } label: {
                HoverIcon(systemName: "plus", isHovered: isAddHovered)
            }
            .buttonStyle(.borderless)
            .disabled(controller.isLoading || controller.addingTarget != nil)
            .accessibilityLabel(L10n.addAccount)
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
            .accessibilityLabel(L10n.refresh)
            .onHover { isRefreshHovered = $0 }
            .nonFocusable()
        }
    }

    @ViewBuilder
    private var loginProgress: some View {
        if controller.addingTarget != nil {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(L10n.signingInAntigravity)
                    .font(.callout)
                    .foregroundColor(.secondary)
                Spacer()
                Button(L10n.cancel) {
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
            if profiles.count > 4 {
                ScrollView {
                    profilesList(profiles)
                        .padding(.horizontal, 2)
                        .padding(.vertical, 2)
                }
                .frame(height: 350)
            } else {
                profilesList(profiles)
                    .padding(.horizontal, 2)
                    .padding(.vertical, 2)
            }
        } else if controller.isLoading {
            Text(L10n.loading)
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 12)
        } else {
            VStack(spacing: 4) {
                Text(L10n.noSavedAccounts)
                    .font(.callout)
                    .foregroundColor(.secondary)
                Text(L10n.addAccountViaPlus)
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 12)
        }
    }

    private func profilesList(_ profiles: [AntigravityProfile]) -> some View {
        let ordered = orderedProfiles(profiles)
        return VStack(spacing: 8) {
            ForEach(ordered) { profile in
                AntigravityAccountCard(
                    profile: profile,
                    isCliActive: controller.status?.active?["cli"] == profile.id,
                    isIdeActive: controller.status?.active?["ide"] == profile.id,
                    isLoading: controller.isLoading,
                    onSwitchCli: { controller.switchTo(profileID: profile.id, target: "cli") },
                    onSwitchIde: { controller.switchTo(profileID: profile.id, target: "ide") },
                    onRemove: { removal = AntigravityRemoval(profileID: profile.id) },
                    onDragChanged: { updateDrag($0, profileID: profile.id, profiles: ordered) },
                    onDragEnded: { finishDrag(profileID: profile.id, profiles: ordered) }
                )
                .background {
                    GeometryReader { geometry in
                        Color.clear.preference(
                            key: AntigravityFramePreferenceKey.self,
                            value: [profile.id: geometry.frame(in: .named("antigravityAccountList"))]
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
                .accessibilityAction(named: Text(L10n.moveUp)) {
                    moveProfile(profile.id, by: -1, profiles: ordered)
                }
                .accessibilityAction(named: Text(L10n.moveDown)) {
                    moveProfile(profile.id, by: 1, profiles: ordered)
                }
            }
        }
        .coordinateSpace(name: "antigravityAccountList")
        .onPreferenceChange(AntigravityFramePreferenceKey.self) { frames in
            if draggedProfileID == nil {
                profileFrames = frames
            }
        }
    }

    private var dropIndicatorAlignment: Alignment {
        dropTargetEdge == .bottom ? .bottom : .top
    }

    @ViewBuilder
    private func dropIndicator(for profileID: String) -> some View {
        if dropTargetID == profileID, let edge = dropTargetEdge {
            Rectangle()
                .fill(Color.accentColor)
                .frame(height: 2)
                .padding(.horizontal, 4)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: edge == .top ? .top : .bottom)
                .allowsHitTesting(false)
        }
    }

    private func orderedProfiles(_ profiles: [AntigravityProfile]) -> [AntigravityProfile] {
        let currentOrder = controller.order["cli"] ?? []
        guard !currentOrder.isEmpty else { return profiles }
        var map = Dictionary(uniqueKeysWithValues: profiles.map { ($0.id, $0) })
        var ordered: [AntigravityProfile] = []
        for id in currentOrder {
            if let p = map.removeValue(forKey: id) {
                ordered.append(p)
            }
        }
        ordered.append(contentsOf: map.values)
        return ordered
    }

    private func updateDrag(
        _ value: DragGesture.Value,
        profileID: String,
        profiles: [AntigravityProfile]
    ) {
        guard draggedProfileID == nil || draggedProfileID == profileID else { return }
        draggedProfileID = profileID
        dragOffset = value.translation.height

        guard let currentFrame = profileFrames[profileID] else { return }
        let currentMidY = currentFrame.midY + dragOffset
        var nearestID: String? = nil
        var nearestDistance: CGFloat = .infinity
        var edge: VerticalEdge = .top

        for (id, frame) in profileFrames where id != profileID {
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

    private func finishDrag(profileID: String, profiles: [AntigravityProfile]) {
        guard draggedProfileID == profileID else { return }
        let targetID = dropTargetID
        let targetEdge = dropTargetEdge

        withAnimation(.easeOut(duration: 0.15)) {
            draggedProfileID = nil
            dragOffset = 0
            dropTargetID = nil
            dropTargetEdge = nil
        }

        guard let targetID, targetID != profileID else { return }
        var ids = profiles.map(\.id)
        guard let sourceIndex = ids.firstIndex(of: profileID),
              let destIndex = ids.firstIndex(of: targetID) else { return }

        ids.remove(at: sourceIndex)
        var insertionIndex = destIndex
        if targetEdge == .bottom {
            insertionIndex = min(insertionIndex + 1, ids.count)
        }
        if sourceIndex < destIndex && targetEdge == .top {
            insertionIndex = max(0, insertionIndex)
        }
        insertionIndex = min(max(0, insertionIndex), ids.count)
        ids.insert(profileID, at: insertionIndex)

        controller.reorder(profileIDs: ids, target: "all")
    }

    private func moveProfile(_ profileID: String, by delta: Int, profiles: [AntigravityProfile]) {
        var ids = profiles.map(\.id)
        guard let index = ids.firstIndex(of: profileID) else { return }
        let newIndex = index + delta
        guard newIndex >= 0, newIndex < ids.count else { return }
        ids.swapAt(index, newIndex)
        controller.reorder(profileIDs: ids, target: "all")
    }
}

// MARK: - Account Card

struct AntigravityAccountCard: View {
    let profile: AntigravityProfile
    let isCliActive: Bool
    let isIdeActive: Bool
    let isLoading: Bool
    let onSwitchCli: () -> Void
    let onSwitchIde: () -> Void
    let onRemove: () -> Void
    let onDragChanged: (DragGesture.Value) -> Void
    let onDragEnded: () -> Void

    @State private var isEmailHovered = false
    @State private var isEmailRevealed = false
    @State private var isActionsHovered = false
    @State private var isDragHandleHovered = false

    private var isFullyActive: Bool { isCliActive && isIdeActive }
    private var isPartiallyActive: Bool { isCliActive || isIdeActive }

    private var activeBadgeText: String? {
        if isCliActive && isIdeActive { return L10n.active }
        if isCliActive { return L10n.activeCli }
        if isIdeActive { return L10n.activeIde }
        return nil
    }

    private var strokeColor: Color {
        if isPartiallyActive { return Color.green.opacity(0.65) }
        return Color.clear
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
                    .accessibilityLabel(L10n.reorderAccount)
                    .accessibilityHint(L10n.dragHint)
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
                    .help(isEmailRevealed ? L10n.hideFullEmail : L10n.showFullEmail)
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
                    if !isCliActive {
                        Button {
                            onSwitchCli()
                        } label: {
                            Label(L10n.switchCliOnly, systemImage: "terminal")
                        }
                    }
                    if !isIdeActive {
                        Button {
                            onSwitchIde()
                        } label: {
                            Label(L10n.switchIdeOnly, systemImage: "chevron.left.forwardslash.chevron.right")
                        }
                    }
                    Divider()
                    Button(L10n.removeFromSwitcher, role: .destructive, action: onRemove)
                } label: {
                    HoverIcon(systemName: "ellipsis", isHovered: isActionsHovered)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .controlSize(.small)
                .disabled(isLoading)
                .accessibilityLabel(L10n.accountActions)
                .onHover { isActionsHovered = $0 }
                .nonFocusable()
            }

            quotaSection
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
                    UsageBarRow(label: L10n.fiveHours, window: short, stale: usage.stale ?? false)
                }
                if let weekly = windows.weekly {
                    UsageBarRow(label: L10n.week, window: weekly, stale: usage.stale ?? false)
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
        Text(L10n.noData)
            .font(.caption)
            .foregroundColor(.secondary)
    }
}
