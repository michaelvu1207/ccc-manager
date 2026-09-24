import AppKit
import ServiceManagement
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    /// Latest snapshot that included this Mac: top-level providers + the
    /// seven-day cost report (WeeklyCostCard reads it).
    @Published private(set) var snapshot: Snapshot?
    /// One row per account with its state on every machine (ccc >= 0.19).
    @Published private(set) var view: AccountsView?
    /// Shared-login sync status (nil with an older ccc).
    @Published private(set) var sync: SyncStatus?
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
            view = snapshot.accountsView
            sync = snapshot.sync
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
        // Each probe answered with a view built from the caches at that
        // moment; one cached re-read afterwards merges them all.
        await loadCached()
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
        if let view = update.accountsView { self.view = view }
        if let sync = update.sync { self.sync = sync }
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
        await perform(key(machine, provider, account), machineID: machine.id) {
            try await self.client.switchAccount(machine: machine.id, provider: provider.id, account: target)
        }
    }

    func setDisabled(_ disabled: Bool, machine: MachineSnapshot, account: Account) async {
        await perform(AccountKey(machine: machine.id, provider: "claude", num: account.num), machineID: machine.id) {
            try await self.client.setDisabled(disabled, machine: machine.id, account: account.num)
        }
    }

    func remove(machine: MachineSnapshot, provider: Provider, account: Account) async {
        confirmingRemoval = nil
        let target = provider.id == "claude" ? account.num : account.email
        await perform(key(machine, provider, account), machineID: machine.id) {
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
        _ key: AccountKey, machineID: String, _ op: @escaping () async throws -> Void
    ) async {
        guard pendingAction == nil else { return }
        pendingAction = key
        defer { pendingAction = nil }
        do {
            try await op()
            errorMessage = nil
            await reloadCached(machineID: machineID)  // ccc patched its cache
        } catch {
            errorMessage = "\(name(ofMachine: machineID)): \(error.localizedDescription)"
            return
        }
        // Usage for the new active account arrives with a live probe.
        Task { await refresh(machineID: machineID) }
    }

    // MARK: - Account-centric actions (machine chips)

    /// Key for the chip of `account` on `machine` (pending / confirm state).
    func chipKey(_ account: ViewAccount, _ machine: String) -> AccountKey {
        AccountKey(machine: machine, provider: account.provider,
                   num: account.presence(on: machine)?.num ?? "+")
    }

    /// The chip's primary action. Never moves a credential between machines:
    /// it switches that machine to its own stored login, or starts a login there.
    func tapChip(_ account: ViewAccount, machine: ViewMachine) async {
        guard let presence = account.presence(on: machine.id) else { return }
        switch presence.state {
        case .stored:
            await switchHere(account, machine: machine.id)
        case .needsLogin:
            reLogin(account, machine: machine.id)
        case .absent:
            if account.provider == "claude" {
                addAccount(machineID: machine.id, email: account.email)
            } else if machine.local {
                openCodexLogin(machineID: machine.id)
            } else {
                errorMessage = "Sign in to Codex as \(account.email) on \(machine.name) with `codex login`."
            }
        case .active, .offline:
            break
        }
    }

    func switchHere(_ account: ViewAccount, machine id: String) async {
        guard let query = account.presence(on: id)?.query else { return }
        await perform(chipKey(account, id), machineID: id) {
            try await self.client.switchAccount(machine: id, provider: account.provider, account: query)
        }
    }

    func reLogin(_ account: ViewAccount, machine id: String) {
        if account.provider == "codex" {
            openCodexLogin(machineID: id)
            return
        }
        if syncEnabled && account.shared == true {
            // Shared login: one fresh login on this Mac replaces it everywhere.
            do {
                try LoginLauncher.openSyncLogin(email: account.email)
            } catch {
                errorMessage = error.localizedDescription
                return
            }
            refreshSoon()
            return
        }
        do {
            try LoginLauncher.openLogin(machine: id, provider: "claude",
                                        num: account.presence(on: id)?.num)
        } catch {
            errorMessage = error.localizedDescription
            return
        }
        refreshLater(id)
    }

    private func openCodexLogin(machineID id: String) {
        guard view?.machines.first(where: { $0.id == id })?.local == true else {
            errorMessage = "Codex login opens a browser; run `codex login` on \(name(ofMachine: id))."
            return
        }
        do {
            try LoginLauncher.openLogin(machine: id, provider: "codex", num: nil)
        } catch {
            errorMessage = error.localizedDescription
            return
        }
        refreshLater(id)
    }

    func setDisabled(_ disabled: Bool, account: ViewAccount, machine id: String) async {
        guard account.provider == "claude", let num = account.presence(on: id)?.num else { return }
        await perform(chipKey(account, id), machineID: id) {
            try await self.client.setDisabled(disabled, machine: id, account: num)
        }
    }

    /// Row menu: every machine that stores the account and is reachable.
    func setDisabledEverywhere(_ disabled: Bool, account: ViewAccount) async {
        for id in account.storedOn where account.presence(on: id)?.state != .offline {
            if account.presence(on: id)?.isDisabled == disabled { continue }
            await setDisabled(disabled, account: account, machine: id)
        }
    }

    func removeFrom(_ account: ViewAccount, machine id: String) async {
        confirmingRemoval = nil
        guard let query = account.presence(on: id)?.query else { return }
        await perform(chipKey(account, id), machineID: id) {
            try await self.client.removeAccount(machine: id, provider: account.provider, account: query)
        }
    }

    func setShared(_ shared: Bool, account: ViewAccount) async {
        do {
            try await client.setShared(shared, email: account.email)
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Sync is set up, so a login on this Mac reaches every machine.
    var syncEnabled: Bool { sync?.enabled == true }

    /// "Log in with new account": with sync, one login on this Mac shared with
    /// every machine; without sync, a login stored on this Mac only.
    func logInNewAccount() {
        do {
            if syncEnabled {
                try LoginLauncher.openSyncLogin(email: nil)
            } else {
                try LoginLauncher.openAddAccount(machine: localMachine?.id ?? "local")
            }
        } catch {
            errorMessage = error.localizedDescription
            return
        }
        refreshSoon()
    }

    /// After a Terminal login: refresh a few times while machines pull it.
    private func refreshSoon() {
        Task {
            for delay in [45.0, 60.0, 90.0] {
                try? await Task.sleep(for: .seconds(delay))
                await refresh()
            }
        }
    }

    func copyEmail(_ account: ViewAccount) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(account.email, forType: .string)
    }

    func addAccount(machineID id: String, email: String? = nil) {
        if let email {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(email, forType: .string)  // for the login page
        }
        do {
            try LoginLauncher.openAddAccount(machine: id)
        } catch {
            errorMessage = error.localizedDescription
            return
        }
        refreshLater(id)
    }

    func saveCurrentClaudeLogin(num: String, id: String) async {
        guard pendingSaveID == nil else { return }
        pendingSaveID = id
        defer { pendingSaveID = nil }
        do {
            try await client.saveCurrentClaudeLogin(num: num)
            await refresh(machineID: localMachine?.id ?? "local")
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func consumeCodexReset(num: String, id: String, creditID: String?) async {
        guard pendingResetID == nil else { return }
        pendingResetID = id
        defer { pendingResetID = nil }
        do {
            try await client.consumeCodexReset(num: num, creditID: creditID)
            await refresh(machineID: localMachine?.id ?? "local")
        } catch {
            errorMessage = error.localizedDescription
        }
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
