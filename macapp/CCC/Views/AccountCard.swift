import SwiftUI

/// One account, shown once however many machines store it. Line 1: the
/// email and one marker per machine column (see MatrixLayout); line 2: the
/// freshest usage from any machine. The row itself does nothing on click:
/// a machine cell switches that machine.
struct AccountCard: View {
    @EnvironmentObject private var state: AppState
    let account: ViewAccount
    let machines: [ViewMachine]
    var layout: MatrixLayout? = nil

    @State private var hovering = false
    @State private var showingResetConfirmation = false

    private var isClaude: Bool { account.provider == "claude" }
    private var providerName: String { isClaude ? "Claude" : "Codex" }
    /// Claude's reset that the server takes now (at a limit it clears).
    private var usableClaudeReset: ResetCredit? {
        account.consumableResetCredits?.first { $0.usableNow == true }
    }
    /// A reset this Mac can use: Codex any time, Claude only at a limit.
    private var canUseReset: Bool {
        guard localPresence?.num != nil, (account.consumableResets ?? 0) > 0 else { return false }
        return !isClaude || usableClaudeReset != nil
    }
    private var grid: MatrixLayout { layout ?? MatrixLayout(machines: machines.count) }
    private var localMachine: ViewMachine? { machines.first { $0.local } }
    private var localPresence: MachinePresence? { localMachine.flatMap { account.presence(on: $0.id) } }
    private var removalMachine: ViewMachine? {
        guard let key = state.confirmingRemoval, key.provider == account.provider else { return nil }
        return machines.first { state.chipKey(account, $0.id) == key && $0.id == key.machine }
    }
    /// Needs a login on at least one machine (or the hub could not refresh it).
    private var needsLoginSomewhere: Bool {
        account.needsLogin || account.machines.values.contains { $0.state == .needsLogin }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 0) {
                    titleLine
                        .frame(width: grid.labelWidth, alignment: .leading)
                    Color.clear.frame(width: MatrixLayout.gap, height: 1)
                    ForEach(machines) { machine in
                        MachineCell(account: account, machine: machine, width: grid.columnWidth)
                    }
                    Color.clear.frame(width: MatrixLayout.gap, height: 1)
                    rowMenu.frame(width: MatrixLayout.menuWidth)
                }
                usageLine
            }
            .padding(.vertical, 5)
            .padding(.horizontal, MatrixLayout.rowInset)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(hovering ? Color.primary.opacity(0.04) : .clear)
            )
            .onHover { hovering = $0 }

            if let machine = removalMachine { removalConfirmation(machine) }
            if showingResetConfirmation { resetConfirmation }
            if state.confirmingDelete == account.key { deleteConfirmation }
        }
    }

    // MARK: Header

    private var titleLine: some View {
        HStack(spacing: 4) {
            Text(account.label)
                .font(.callout)
                .fontWeight(account.activeOn.isEmpty ? .regular : .semibold)
                .foregroundStyle(account.disabled ? .secondary : .primary)
                .lineLimit(1)
                .truncationMode(.middle)
                .help(titleHelp)
            if account.disabled {
                Text("(disabled)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
                    .help("Held out of auto-rotation on every machine that stores it")
            } else if account.mixedDisabled {
                Text("(\(account.disabledOn.count) off)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
                    .help("Disabled on: " + account.disabledOn.map { state.name(ofMachine: $0) }.joined(separator: ", "))
            }
        }
    }

    private var titleHelp: String {
        var text = account.email
        if !account.plan.isEmpty { text += " · \(account.plan)" }
        if !account.orgName.isEmpty && !account.orgName.hasPrefix(account.email) { text += " · \(account.orgName)" }
        if account.shared == true { text += " · shared login, the hub keeps it fresh" }
        if account.hubDeleted == true { text += " · deleted on the hub (copies left here)" }
        return text
    }

    /// Usage (line 2) with the per-account buttons that need room.
    private var usageLine: some View {
        HStack(alignment: .top, spacing: 6) {
            VStack(alignment: .leading, spacing: 2) { usage }
                .frame(maxWidth: .infinity, alignment: .leading)
            trailingControls
        }
    }

    @ViewBuilder
    private var usage: some View {
        if account.needsLogin {
            Label("needs login on every machine that stores it", systemImage: "exclamationmark.triangle.fill")
                .font(.caption2)
                .foregroundStyle(.orange)
        } else if account.windows.isEmpty {
            if !account.statusMessage.isEmpty {
                Label(account.statusMessage,
                      systemImage: account.usagePending == true ? "clock" : "exclamationmark.circle")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        } else {
            ForEach(account.windows) { window in
                UsageBar(window: window)
            }
            if let caption = sourceCaption {
                Text(caption)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        if let resets = account.consumableResets {
            Label("\(resets) usage limit reset\(resets == 1 ? "" : "s") available"
                  + (isClaude && usableClaudeReset != nil ? " · usable now" : ""),
                  systemImage: "arrow.counterclockwise.circle.fill")
                .font(.caption2.weight(.medium))
                .foregroundStyle(resets > 0 ? Color.green : Color.secondary)
        }
    }

    /// "last known 5m ago from work-pc" — only when the usage is stale.
    private var sourceCaption: String? {
        guard let from = account.usageFrom, account.usageStale else { return nil }
        var text = account.usageStale ? "last known" : "as of"
        if let at = account.usageAt { text += " \(MachineSection.age(since: at)) ago" }
        return text + " from \(state.name(ofMachine: from))"
    }

    // MARK: Trailing controls (re-login, 0.17 save / reset) and the row menu

    private var trailingControls: some View {
        HStack(spacing: 6) {
            if needsLoginSomewhere {
                Button {
                    state.reLoginAccount(account)
                } label: {
                    Label("Re-login", systemImage: "person.badge.key")
                        .font(.caption2)
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.orange)
                .help(reLoginHelp)
            }
            if isClaude, let local = localMachine, let num = localPresence?.num {
                Button {
                    Task { await state.saveCurrentClaudeLogin(num: num, id: account.key) }
                } label: {
                    if state.pendingSaveID == account.key {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "square.and.arrow.down")
                            .font(.caption)
                    }
                }
                .buttonStyle(.borderless)
                .help("Save the current Claude login on \(local.name) for \(account.email)")
                .accessibilityLabel("Save current Claude login for \(account.email)")
                .opacity(hovering ? 1 : 0)
            }
            if canUseReset {
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) { showingResetConfirmation.toggle() }
                } label: {
                    if state.pendingResetID == account.key {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "arrow.counterclockwise.circle")
                    }
                }
                .buttonStyle(.borderless)
                .disabled(state.pendingResetID != nil)
                .help("Use a \(providerName) usage-limit reset for \(account.email)")
                .accessibilityLabel("Use a \(providerName) usage-limit reset for \(account.email)")
            }
        }
    }

    private var reLoginHelp: String {
        switch AppState.reloginPlan(account: account, machines: machines, syncEnabled: state.syncEnabled) {
        case .syncLogin:
            return "Opens Terminal: sign in as \(account.email) on this Mac; the hub replaces the login on every machine"
        case .machineLogin(let id, _):
            return "Opens Terminal: sign in as \(account.email) on \(state.name(ofMachine: id))"
        case .codex:
            return "Opens the Codex login in your browser"
        case .guidance(let text):
            return text
        }
    }

    private var rowMenu: some View {
        Menu {
            Button("Re-login…") { state.reLoginAccount(account) }
            if isClaude && !account.storedOn.isEmpty {
                Divider()
                Button("Disable everywhere") {
                    Task { await state.setDisabledEverywhere(true, account: account) }
                }
                .disabled(account.disabled)
                Button("Enable everywhere") {
                    Task { await state.setDisabledEverywhere(false, account: account) }
                }
                .disabled(account.disabledOn.isEmpty)
            }
            if let shared = account.shared {
                Button(shared ? "Stop sharing this login" : "Share this login with all machines") {
                    Task { await state.setShared(!shared, account: account) }
                }
            }
            Divider()
            Button("Copy email") { state.copyEmail(account) }
            Divider()
            Button("Delete account…", role: .destructive) {
                withAnimation(.easeInOut(duration: 0.15)) { state.confirmingDelete = account.key }
            }
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .frame(width: MatrixLayout.menuWidth, height: 16)
                .contentShape(Rectangle())
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .opacity(hovering || state.confirmingDelete == account.key ? 1 : 0)
        .allowsHitTesting(hovering)
        .help("Account actions")
        .accessibilityLabel("Actions for \(account.email)")
    }

    // MARK: Confirmations

    private func removalConfirmation(_ machine: ViewMachine) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Remove \(account.email) from \(machine.name)?")
                .font(.caption.weight(.semibold))
            Text("This deletes the saved login on that machine only. Other machines keep theirs.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { state.confirmingRemoval = nil }
                    .keyboardShortcut(.cancelAction)
                Button("Remove", role: .destructive) {
                    Task { await state.removeFrom(account, machine: machine.id) }
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
            }
            .controlSize(.small)
        }
        .padding(8)
        .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.red.opacity(0.25), lineWidth: 1))
        .padding(.horizontal, 8)
        .padding(.bottom, 4)
    }

    private var deleteConfirmation: some View {
        let deleting = state.pendingDelete == account.key
        return VStack(alignment: .leading, spacing: 6) {
            Text("Delete \(account.email) everywhere?")
                .font(.caption.weight(.semibold))
            if let warning = AppState.deleteWarning(
                account: account, name: { state.name(ofMachine: $0) },
                syncing: state.syncEnabled && account.shared != nil) {
                Text(warning)
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(state.syncEnabled && account.shared != nil
                 ? "The hub forgets the login and every machine drops its copy. Log in to it again to bring it back."
                 : "Every machine that stores it removes its saved login.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                if deleting {
                    ProgressView().controlSize(.mini)
                    Text("Deleting…").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel") { state.confirmingDelete = nil }
                    .keyboardShortcut(.cancelAction)
                    .disabled(deleting)
                Button("Delete", role: .destructive) {
                    Task { await state.deleteAccount(account) }
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .disabled(deleting)
            }
            .controlSize(.small)
        }
        .padding(8)
        .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.red.opacity(0.25), lineWidth: 1))
        .padding(.horizontal, 8)
        .padding(.bottom, 4)
    }

    private var resetConfirmation: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Use one reset for \(account.email)? This immediately resets its \(providerName) usage limits and cannot be undone.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if isClaude, let credit = usableClaudeReset {
                resetButton(label: credit.actionLabel, creditID: nil)
            } else if let credits = account.consumableResetCredits, !credits.isEmpty {
                ForEach(credits) { credit in resetButton(label: credit.actionLabel, creditID: credit.id) }
            } else {
                resetButton(label: "Use one reset", creditID: nil)
            }
            Button("Cancel") {
                withAnimation(.easeInOut(duration: 0.15)) { showingResetConfirmation = false }
            }
            .buttonStyle(.plain)
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
        .padding(8)
        .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.red.opacity(0.25), lineWidth: 1))
        .padding(.horizontal, 8)
        .padding(.bottom, 4)
    }

    private func resetButton(label: String, creditID: String?) -> some View {
        Button("Use \(label)", role: .destructive) {
            showingResetConfirmation = false
            guard let num = localPresence?.num else { return }
            Task { await state.consumeReset(provider: account.provider, num: num, id: account.key, creditID: creditID) }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .tint(.red)
        .disabled(state.pendingResetID != nil)
        .accessibilityHint("Consumes one saved reset and cannot be undone")
    }
}
