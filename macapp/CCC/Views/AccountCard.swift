import SwiftUI

/// One account, shown once however many machines store it: identity, usage
/// (the freshest report from any machine), and a chip per machine.
struct AccountCard: View {
    @EnvironmentObject private var state: AppState
    let account: ViewAccount
    let machines: [ViewMachine]

    @State private var hovering = false
    @State private var showingResetConfirmation = false

    private var isClaude: Bool { account.provider == "claude" }
    private var localMachine: ViewMachine? { machines.first { $0.local } }
    private var localPresence: MachinePresence? { localMachine.flatMap { account.presence(on: $0.id) } }
    private var removalMachine: ViewMachine? {
        guard let key = state.confirmingRemoval, key.provider == account.provider else { return nil }
        return machines.first { state.chipKey(account, $0.id) == key && $0.id == key.machine }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: account.activeOn.isEmpty ? "circle" : "circle.fill")
                    .font(.system(size: 8))
                    .foregroundStyle(account.activeOn.isEmpty ? Color.secondary : Color.accentColor)
                    .padding(.top, 4)
                    .help(activeHelp)

                VStack(alignment: .leading, spacing: 3) {
                    titleLine
                    if !account.orgName.isEmpty && !account.orgName.hasPrefix(account.email) {
                        Text(account.orgName)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    usage
                    FlowLayout(spacing: 4, lineSpacing: 4) {
                        ForEach(machines) { machine in
                            MachineChip(account: account, machine: machine)
                        }
                    }
                    .padding(.top, 2)
                }
                Spacer(minLength: 0)
                trailingControls
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 8)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(hovering ? Color.primary.opacity(0.05) : .clear)
            )
            .onHover { hovering = $0 }

            if let machine = removalMachine { removalConfirmation(machine) }
            if showingResetConfirmation { resetConfirmation }
        }
    }

    // MARK: Header

    private var titleLine: some View {
        HStack(spacing: 5) {
            Text(account.label)
                .font(.callout)
                .fontWeight(account.activeOn.isEmpty ? .regular : .semibold)
                .foregroundStyle(account.disabled ? .secondary : .primary)
                .lineLimit(1)
                .truncationMode(.middle)
            if account.disabled {
                Text("(disabled)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .help("Held out of auto-rotation on every machine that stores it")
            } else if account.mixedDisabled {
                Text("(disabled on \(account.disabledOn.count))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .help("Disabled on: " + account.disabledOn.map { state.name(ofMachine: $0) }.joined(separator: ", "))
            }
            if account.shared == true {
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.caption2)
                    .foregroundStyle(account.refreshError?.isEmpty == false ? Color.orange : Color.secondary)
                    .help(account.refreshError?.isEmpty == false
                          ? "Shared login · hub refresh failed: \(account.refreshError ?? "")"
                          : "Shared login · the hub keeps it fresh on every machine (gen \(account.hubGeneration ?? 0))")
            }
            if !account.plan.isEmpty {
                Text(account.plan)
                    .font(.caption2)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.quaternary, in: Capsule())
                    .foregroundStyle(.secondary)
            }
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
            Label("\(resets) usage limit reset\(resets == 1 ? "" : "s") available",
                  systemImage: "arrow.counterclockwise.circle.fill")
                .font(.caption2.weight(.medium))
                .foregroundStyle(resets > 0 ? Color.green : Color.secondary)
        }
    }

    /// "as of 5m ago from path-pc" — only when it matters (stale or remote).
    private var sourceCaption: String? {
        guard let from = account.usageFrom else { return nil }
        let isLocal = machines.first { $0.id == from }?.local ?? false
        guard account.usageStale || !isLocal else { return nil }
        var text = account.usageStale ? "last known" : "as of"
        if let at = account.usageAt { text += " \(MachineSection.age(since: at)) ago" }
        return text + " from \(state.name(ofMachine: from))"
    }

    private var activeHelp: String {
        account.activeOn.isEmpty
            ? "Not the active login anywhere"
            : "Active on " + account.activeOn.map { state.name(ofMachine: $0) }.joined(separator: ", ")
    }

    // MARK: Trailing controls (0.17 save / reset + row menu)

    private var trailingControls: some View {
        HStack(spacing: 6) {
            if isClaude, let local = localMachine, let num = localPresence?.num {
                Button {
                    Task { await state.saveCurrentClaudeLogin(num: num, id: account.key) }
                } label: {
                    if state.pendingSaveID == account.key {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "square.and.arrow.down")
                    }
                }
                .buttonStyle(.borderless)
                .help("Save the current Claude login on \(local.name) for \(account.email)")
                .accessibilityLabel("Save current Claude login for \(account.email)")
                .opacity(hovering ? 1 : 0.55)
            } else if !isClaude, localPresence?.num != nil, (account.consumableResets ?? 0) > 0 {
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
                .help("Use a Codex usage-limit reset for \(account.email)")
            }
            Menu {
                if isClaude && !account.storedOn.isEmpty {
                    Button("Disable everywhere") {
                        Task { await state.setDisabledEverywhere(true, account: account) }
                    }
                    .disabled(account.disabled)
                    Button("Enable everywhere") {
                        Task { await state.setDisabledEverywhere(false, account: account) }
                    }
                    .disabled(account.disabledOn.isEmpty)
                    Divider()
                }
                if let shared = account.shared {
                    Button(shared ? "Stop sharing this login" : "Share this login with all machines") {
                        Task { await state.setShared(!shared, account: account) }
                    }
                    Divider()
                }
                Button("Copy email") { state.copyEmail(account) }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 18, height: 16)
                    .contentShape(Rectangle())
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .opacity(hovering ? 1 : 0)
            .allowsHitTesting(hovering)
            .help("Account actions")
        }
        .padding(.top, 1)
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

    private var resetConfirmation: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Use one reset for \(account.email)? This immediately resets its Codex usage limits and cannot be undone.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let credits = account.consumableResetCredits, !credits.isEmpty {
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
            Task { await state.consumeCodexReset(num: num, id: account.key, creditID: creditID) }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .tint(.red)
        .disabled(state.pendingResetID != nil)
        .accessibilityHint("Consumes one saved reset and cannot be undone")
    }
}
