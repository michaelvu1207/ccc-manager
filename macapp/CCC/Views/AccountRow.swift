import SwiftUI

struct AccountRow: View {
    @EnvironmentObject private var state: AppState
    let machine: MachineSnapshot
    let provider: Provider
    let account: Account

    @State private var hovering = false
    @State private var showingResetConfirmation = false

    private var key: AccountKey { state.key(machine, provider, account) }
    private var isPending: Bool { state.pendingAction == key }
    private var isSaving: Bool { state.pendingSaveID == account.id && machine.local }
    private var isResetting: Bool { state.pendingResetID == account.id && machine.local }
    private var isClaude: Bool { provider.id == "claude" }
    private var confirmingRemoval: Bool { state.confirmingRemoval == key }
    /// Codex logins open a browser, so they only work on this Mac.
    private var canLogin: Bool { isClaude || machine.local }
    /// Codex accounts are removed with codex-auth on cswap-engine machines.
    private var canRemove: Bool { isClaude || machine.local || machine.engine == "ccc" }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 0) {
                mainButton
                trailingControls
                    .padding(.top, 5)
                    .padding(.trailing, 6)
            }
            .onHover { hovering = $0 }
            .contextMenu { menuItems }

            if showingResetConfirmation {
                resetConfirmation
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
            if confirmingRemoval { removalConfirmation }
        }
    }

    // MARK: Trailing controls

    @ViewBuilder
    private var trailingControls: some View {
        HStack(spacing: 6) {
            if machine.local && isClaude {
                Button {
                    Task { await state.saveCurrentClaudeLogin(account: account) }
                } label: {
                    if isSaving {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "square.and.arrow.down")
                    }
                }
                .buttonStyle(.borderless)
                .disabled(isSaving)
                .help("Save the current Claude login for \(account.email)")
                .accessibilityLabel("Save current Claude login for \(account.email)")
            } else if machine.local && (account.consumableResets ?? 0) > 0 {
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) {
                        showingResetConfirmation.toggle()
                    }
                } label: {
                    if isResetting {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "arrow.counterclockwise.circle")
                    }
                }
                .buttonStyle(.borderless)
                .disabled(isResetting || state.pendingResetID != nil)
                .help("Use a Codex usage-limit reset for \(account.email)")
                .accessibilityLabel("Use a Codex usage-limit reset for \(account.email)")
            }
            actionsMenu
                .opacity(hovering || isPending ? 1 : 0)
                .allowsHitTesting(hovering)
        }
    }

    private var actionsMenu: some View {
        Menu {
            menuItems
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
        .help("Account actions")
        .accessibilityLabel("Actions for \(account.email)")
    }

    @ViewBuilder
    private var menuItems: some View {
        Button(account.active ? "Active on \(machine.displayName)" : "Switch here") {
            Task { await state.switchTo(machine: machine, provider: provider, account: account) }
        }
        .disabled(account.active || account.needsLogin)

        if canLogin {
            Button("Re-login…") {
                state.login(machine: machine, provider: provider, account: account)
            }
        }
        if isClaude {
            Button(account.isDisabled ? "Enable (return to rotation)" : "Disable (hold out of rotation)") {
                Task { await state.setDisabled(!account.isDisabled, machine: machine, account: account) }
            }
        }
        if canRemove {
            Divider()
            Button("Remove…", role: .destructive) {
                state.confirmingRemoval = key
            }
        }
    }

    // MARK: Main row

    private var mainButton: some View {
        Button {
            if account.needsLogin {
                if canLogin { state.login(machine: machine, provider: provider, account: account) }
            } else {
                Task { await state.switchTo(machine: machine, provider: provider, account: account) }
            }
        } label: {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: account.active ? "circle.fill" : "circle")
                    .font(.system(size: 8))
                    .foregroundStyle(account.active ? Color.accentColor : Color.secondary)
                    .padding(.top, 4)

                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 5) {
                        Text(account.label)
                            .font(.callout)
                            .fontWeight(account.active ? .semibold : .regular)
                            .foregroundStyle(account.isDisabled ? .secondary : .primary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        if account.isDisabled {
                            Text("(disabled)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .help("Held out of auto-rotation")
                        }
                        if !account.plan.isEmpty {
                            Text(account.plan)
                                .font(.caption2)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(.quaternary, in: Capsule())
                                .foregroundStyle(.secondary)
                        }
                        if isPending {
                            ProgressView().controlSize(.mini)
                        }
                    }

                    if account.needsLogin {
                        Label(canLogin ? "needs login — click to re-authenticate"
                                       : "needs login — run codex login on that machine",
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                    } else {
                        ForEach(account.windows) { window in
                            UsageBar(window: window)
                        }
                        if let resets = account.consumableResets {
                            Label(
                                "\(resets) usage limit reset\(resets == 1 ? "" : "s") available",
                                systemImage: "arrow.counterclockwise.circle.fill"
                            )
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(resets > 0 ? Color.green : Color.secondary)
                            .accessibilityLabel(
                                "\(resets) consumable usage limit reset\(resets == 1 ? "" : "s") available"
                            )
                        }
                        if account.usageStale && !account.windows.isEmpty {
                            Text(machine.local ? "usage data is stale" : "last known usage")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        } else if account.windows.isEmpty,
                                  let message = account.statusMessage,
                                  !message.isEmpty {
                            Label(message, systemImage: account.status == "keychain_unavailable"
                                  ? "lock" : "exclamationmark.circle")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 5)
            .padding(.horizontal, 8)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(hovering ? Color.primary.opacity(0.07) : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(helpText)
    }

    private var helpText: String {
        if account.active { return "Active account on \(machine.displayName)" }
        if account.needsLogin {
            return canLogin ? "Open Terminal to re-login" : "Log in on \(machine.displayName) with codex login"
        }
        return "Switch \(machine.displayName) to \(account.email)"
    }

    // MARK: Confirmations

    private var removalConfirmation: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Remove \(account.email) from \(machine.displayName)?")
                .font(.caption.weight(.semibold))
            Text("This deletes the account's saved login on that machine. You can add it again later.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { state.confirmingRemoval = nil }
                    .keyboardShortcut(.cancelAction)
                Button("Remove", role: .destructive) {
                    Task { await state.remove(machine: machine, provider: provider, account: account) }
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
            }
            .controlSize(.small)
        }
        .padding(8)
        .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(Color.red.opacity(0.25), lineWidth: 1)
        )
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
                ForEach(credits) { credit in
                    resetActionButton(label: credit.actionLabel, creditID: credit.id)
                }
            } else {
                resetActionButton(label: "Use one reset", creditID: nil)
            }

            Button("Cancel") {
                withAnimation(.easeInOut(duration: 0.15)) {
                    showingResetConfirmation = false
                }
            }
            .buttonStyle(.plain)
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
        .padding(8)
        .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(Color.red.opacity(0.25), lineWidth: 1)
        )
        .padding(.horizontal, 8)
        .padding(.bottom, 4)
    }

    private func resetActionButton(label: String, creditID: String?) -> some View {
        Button("Use \(label)", role: .destructive) {
            showingResetConfirmation = false
            Task {
                await state.consumeCodexReset(account: account, creditID: creditID)
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .tint(.red)
        .disabled(isResetting || state.pendingResetID != nil)
        .accessibilityHint("Consumes one saved reset and cannot be undone")
    }
}
