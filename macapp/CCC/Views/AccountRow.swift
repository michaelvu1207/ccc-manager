import SwiftUI

struct AccountRow: View {
    @EnvironmentObject private var state: AppState
    let provider: Provider
    let account: Account

    @State private var hovering = false
    @State private var showingResetConfirmation = false

    private var isPending: Bool { state.pendingSwitchID == account.id }
    private var isSaving: Bool { state.pendingSaveID == account.id }
    private var isResetting: Bool { state.pendingResetID == account.id }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 0) {
                mainButton
                if provider.id == "claude" {
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
                    .padding(.trailing, 8)
                } else if (account.consumableResets ?? 0) > 0 {
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
                    .padding(.trailing, 8)
                }
            }

            if showingResetConfirmation {
                resetConfirmation
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
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

    private var mainButton: some View {
        Button {
            if account.needsLogin {
                state.login(provider: provider, account: account)
            } else {
                Task { await state.switchTo(provider: provider, account: account) }
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
                            .lineLimit(1)
                            .truncationMode(.middle)
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
                        Label("needs login — click to re-authenticate", systemImage: "exclamationmark.triangle.fill")
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
                        if account.usageStale {
                            Text("usage data is stale")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        } else if account.windows.isEmpty,
                                  let message = account.statusMessage,
                                  !message.isEmpty {
                            Label(message, systemImage: "exclamationmark.circle")
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
        .onHover { hovering = $0 }
        .help(account.active
            ? "Active account"
            : account.needsLogin ? "Open Terminal to re-login" : "Switch to \(account.email)")
    }
}
