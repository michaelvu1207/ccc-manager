import AppKit
import ServiceManagement
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var isRefreshing = false
    @Published var lastRefreshed: Date?
    @Published var errorMessage: String?
    @Published var launchAtLogin: Bool = SMAppService.mainApp.status == .enabled
    /// Account id being switched to, for optimistic UI.
    @Published var pendingSwitchID: String?
    @Published var pendingSaveID: String?
    @Published var pendingResetID: String?

    private let client = CCCClient.shared
    private var refreshTimer: Timer?

    static let refreshInterval: TimeInterval = 300

    init() {
        Task {
            await loadCached()
            await refresh()
        }
        refreshTimer = Timer.scheduledTimer(
            withTimeInterval: Self.refreshInterval, repeats: true
        ) { [weak self] _ in
            Task { await self?.refresh() }
        }
        // The popover is our only window; when it opens (becomes key), repaint
        // from cache instantly and kick a live refresh if data is stale.
        NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { await self?.popoverOpened() }
        }
    }

    // MARK: - Menu bar label

    var menuBarTitle: String {
        guard let claude = snapshot?.provider("claude"), let active = claude.activeAccount else {
            return ""
        }
        if active.needsLogin { return "!" }
        guard let left = active.window("5h")?.pctLeft else { return "" }
        return "\(Int(left))%"
    }

    var menuBarSymbol: String {
        let active = snapshot?.provider("claude")?.activeAccount
        if active?.needsLogin == true { return "exclamationmark.triangle.fill" }
        return "circle.lefthalf.filled"
    }

    // MARK: - Loading

    func loadCached() async {
        do {
            snapshot = try await client.snapshot(refresh: false)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            snapshot = try await client.snapshot(refresh: true)
            lastRefreshed = Date()
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func popoverOpened() async {
        await loadCached()
        if let last = lastRefreshed, Date().timeIntervalSince(last) < 120 { return }
        await refresh()
    }

    // MARK: - Actions

    func switchTo(provider: Provider, account: Account) async {
        guard !account.active, pendingSwitchID == nil else { return }
        pendingSwitchID = account.id
        defer { pendingSwitchID = nil }
        do {
            try await client.switchAccount(provider: provider.id, num: account.num)
            await loadCached()  // active flag updates on disk immediately
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func login(provider: Provider, account: Account) {
        do {
            // codex picks the account in the browser; claude needs the slot
            try LoginLauncher.openLogin(
                provider: provider.id,
                num: provider.id == "claude" ? account.num : nil
            )
        } catch {
            errorMessage = error.localizedDescription
            return
        }
        // Pick up restored credentials once the user has had time to finish.
        Task {
            try? await Task.sleep(for: .seconds(90))
            await refresh()
        }
    }

    func saveCurrentClaudeLogin(account: Account) async {
        guard pendingSaveID == nil else { return }
        pendingSaveID = account.id
        defer { pendingSaveID = nil }
        do {
            try await client.saveCurrentClaudeLogin(num: account.num)
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func consumeCodexReset(account: Account, creditID: String?) async {
        guard pendingResetID == nil else { return }
        pendingResetID = account.id
        defer { pendingResetID = nil }
        do {
            try await client.consumeCodexReset(num: account.num, creditID: creditID)
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            launchAtLogin = SMAppService.mainApp.status == .enabled
        } catch {
            errorMessage = "launch at login: \(error.localizedDescription)"
            launchAtLogin = SMAppService.mainApp.status == .enabled
        }
    }
}
