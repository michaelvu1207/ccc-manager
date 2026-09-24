import AppKit
import ServiceManagement
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    /// Latest snapshot that included this Mac: top-level providers + the
    /// seven-day cost report (WeeklyCostCard reads it).
    @Published private(set) var snapshot: Snapshot?
    /// Machines in display order (this Mac first), each with its accounts.
    @Published private(set) var machines: [MachineSnapshot] = []
    /// Machine ids with a live probe in flight.
    @Published private(set) var loadingMachines: Set<String> = []
    @Published private(set) var isRefreshing = false
    @Published private(set) var lastRefreshed: Date?
    @Published var errorMessage: String?
    @Published var launchAtLogin: Bool = SMAppService.mainApp.status == .enabled
    /// Account being switched / enabled / removed, for inline progress.
    @Published private(set) var pendingAction: AccountKey?
    /// Account whose "Remove…" awaits the inline confirmation.
    @Published var confirmingRemoval: AccountKey?
    @Published private(set) var collapsed: Set<String>
    @Published var showingMachines = false
    @Published private(set) var machineList: MachineList?
    @Published private(set) var hasLoaded = false
    @Published private(set) var pendingSaveID: String?
    @Published private(set) var pendingResetID: String?

    private let client = CCCClient.shared
    private var refreshTimer: Timer?

    static let refreshInterval: TimeInterval = 300
    private static let collapsedKey = "collapsedMachines"

    /// Unit tests host the app; never touch real accounts from there.
    nonisolated static var isRunningTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }

    init() {
        collapsed = Set(UserDefaults.standard.stringArray(forKey: Self.collapsedKey) ?? [])
        guard !Self.isRunningTests else { return }
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

    // MARK: - Derived

    var localMachine: MachineSnapshot? { machines.first { $0.local } }

    /// Machines shown in the popover: duplicates of another entry are hidden.
    var visibleMachines: [MachineSnapshot] { machines.filter { $0.duplicateOf == nil } }

    /// Human name for "same machine as …" notes.
    func twinName(_ id: String) -> String {
        machines.first { $0.id == id }?.local == true ? "this Mac" : name(ofMachine: id)
    }
    var hiddenDuplicates: [MachineSnapshot] { machines.filter { $0.duplicateOf != nil } }

    func name(ofMachine id: String) -> String {
        machines.first { $0.id == id }?.displayName
            ?? machineList?.machines.first { $0.id == id }?.name ?? id
    }

    func isLoading(_ machine: MachineSnapshot) -> Bool { loadingMachines.contains(machine.id) }

    // MARK: - Menu bar label

    var menuBarTitle: String {
        guard let active = (localMachine?.provider("claude") ?? snapshot?.provider("claude"))?
            .activeAccount else { return "" }
        if active.needsLogin { return "!" }
        guard let left = active.window("5h")?.pctLeft else { return "" }
        return "\(Int(left))%"
    }

    var menuBarSymbol: String {
        let active = localMachine?.provider("claude")?.activeAccount
        if active?.needsLogin == true { return "exclamationmark.triangle.fill" }
        return "circle.lefthalf.filled"
    }

    // MARK: - Collapse

    func isCollapsed(_ machine: MachineSnapshot) -> Bool { collapsed.contains(machine.id) }

    func toggleCollapsed(_ machine: MachineSnapshot) {
        if collapsed.contains(machine.id) {
            collapsed.remove(machine.id)
        } else {
            collapsed.insert(machine.id)
        }
        UserDefaults.standard.set(Array(collapsed), forKey: Self.collapsedKey)
    }

    // MARK: - Loading

    /// Repaint every machine from ccc's caches (fast, no network).
    func loadCached() async {
        do {
            let snapshot = try await client.snapshot(refresh: false)
            self.snapshot = snapshot
            machines = snapshot.machineList
            hasLoaded = true
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Probe every enabled machine in parallel; each section updates as soon
    /// as its machine answers, so one slow host never holds up the rest.
    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        if machines.isEmpty { await loadCached() }
        let ids = machines.map(\.id)
        await withTaskGroup(of: Void.self) { group in
            for id in ids {
                group.addTask { await self.refresh(machineID: id) }
            }
        }
        lastRefreshed = Date()
    }

    func refresh(machineID id: String) async {
        guard !loadingMachines.contains(id) else { return }
        loadingMachines.insert(id)
        defer { loadingMachines.remove(id) }
        do {
            let snapshot = try await client.snapshot(refresh: true, machines: [id])
            absorb(snapshot)
            errorMessage = nil
        } catch {
            errorMessage = "\(name(ofMachine: id)): \(error.localizedDescription)"
        }
    }

    /// Re-read one machine from cache (after an action patched it).
    private func reloadCached(machineID id: String) async {
        if let snapshot = try? await client.snapshot(refresh: false, machines: [id]) {
            absorb(snapshot)
        }
    }

    /// Take a (possibly machine-limited) snapshot: machines merge in place;
    /// a snapshot that includes this Mac also replaces `snapshot` (costs).
    private func absorb(_ update: Snapshot) {
        if let machines = update.machines {
            merge(machines)
            if machines.contains(where: { $0.local }) { snapshot = update }
        } else {
            snapshot = update  // older ccc: flat, this Mac only
            merge(update.machineList)
        }
    }

    private func merge(_ updates: [MachineSnapshot]) {
        for update in updates {
            if let index = machines.firstIndex(where: { $0.id == update.id }) {
                machines[index] = update
            } else {
                machines.append(update)
            }
        }
    }

    private func popoverOpened() async {
        await loadCached()
        if let last = lastRefreshed, Date().timeIntervalSince(last) < 120 { return }
        await refresh()
    }

    // MARK: - Account actions

    func key(_ machine: MachineSnapshot, _ provider: Provider, _ account: Account) -> AccountKey {
        AccountKey(machine: machine.id, provider: provider.id, num: account.num)
    }

    func switchTo(machine: MachineSnapshot, provider: Provider, account: Account) async {
        guard !account.active, pendingAction == nil else { return }
        // codex-auth matches by email; cswap by slot number.
        let target = provider.id == "claude" ? account.num : account.email
        await perform(key(machine, provider, account), machine: machine) {
            try await self.client.switchAccount(machine: machine.id, provider: provider.id, account: target)
        }
    }

    func setDisabled(_ disabled: Bool, machine: MachineSnapshot, account: Account) async {
        await perform(AccountKey(machine: machine.id, provider: "claude", num: account.num), machine: machine) {
            try await self.client.setDisabled(disabled, machine: machine.id, account: account.num)
        }
    }

    func remove(machine: MachineSnapshot, provider: Provider, account: Account) async {
        confirmingRemoval = nil
        let target = provider.id == "claude" ? account.num : account.email
        await perform(key(machine, provider, account), machine: machine) {
            try await self.client.removeAccount(machine: machine.id, provider: provider.id, account: target)
        }
    }

    // MARK: - This Mac only (0.17 actions)

    func saveCurrentClaudeLogin(account: Account) async {
        guard pendingSaveID == nil else { return }
        pendingSaveID = account.id
        defer { pendingSaveID = nil }
        do {
            try await client.saveCurrentClaudeLogin(num: account.num)
            await refresh(machineID: localMachine?.id ?? "local")
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
            await refresh(machineID: localMachine?.id ?? "local")
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func perform(
        _ key: AccountKey, machine: MachineSnapshot, _ op: @escaping () async throws -> Void
    ) async {
        guard pendingAction == nil else { return }
        pendingAction = key
        defer { pendingAction = nil }
        do {
            try await op()
            errorMessage = nil
            await reloadCached(machineID: machine.id)  // ccc patched its cache
        } catch {
            errorMessage = "\(machine.displayName): \(error.localizedDescription)"
            return
        }
        // Usage for the new active account arrives with a live probe.
        Task { await refresh(machineID: machine.id) }
    }

    func login(machine: MachineSnapshot, provider: Provider, account: Account) {
        do {
            try LoginLauncher.openLogin(
                machine: machine.id, provider: provider.id,
                num: provider.id == "claude" ? account.num : nil
            )
        } catch {
            errorMessage = error.localizedDescription
            return
        }
        refreshLater(machine.id)
    }

    func addAccount(machine: MachineSnapshot) {
        do {
            try LoginLauncher.openAddAccount(machine: machine.id)
        } catch {
            errorMessage = error.localizedDescription
            return
        }
        refreshLater(machine.id)
    }

    /// Pick up new credentials once the user has had time to finish in Terminal.
    private func refreshLater(_ id: String) {
        Task {
            try? await Task.sleep(for: .seconds(90))
            await refresh(machineID: id)
        }
    }

    // MARK: - Machines settings

    func loadMachineList() async {
        do {
            machineList = try await client.machineList()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func addMachine(alias: String, name: String) async -> Bool {
        var args = ["add", "--", alias.trimmingCharacters(in: .whitespaces)]
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty { args = ["add", "--name", trimmed, "--", alias.trimmingCharacters(in: .whitespaces)] }
        return await editMachines(args, refreshNew: true)
    }

    func removeMachine(_ machine: MachineConfig) async {
        _ = await editMachines(["remove", "--", machine.id], refreshNew: false)
    }

    func renameMachine(_ machine: MachineConfig, to name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, trimmed != machine.name else { return }
        _ = await editMachines(["rename", "--", machine.id, trimmed], refreshNew: false)
    }

    func setPolling(_ enabled: Bool, machine: MachineConfig) async {
        _ = await editMachines([enabled ? "enable" : "disable", "--", machine.id], refreshNew: enabled)
    }

    func importFromStarline() async {
        _ = await editMachines(["import-starline"], refreshNew: true)
    }

    /// Run `ccc machines …`, then repaint; probe machines that just appeared.
    private func editMachines(_ args: [String], refreshNew: Bool) async -> Bool {
        let before = Set(machines.map(\.id))
        do {
            try await client.editMachines(args)
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
        errorMessage = nil
        await loadMachineList()
        await loadCached()
        if refreshNew {
            for id in machines.map(\.id) where !before.contains(id) {
                Task { await refresh(machineID: id) }
            }
        }
        return true
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
