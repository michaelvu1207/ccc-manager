import SwiftUI

struct AccountRow: View {
    @EnvironmentObject private var state: AppState
    let provider: Provider
    let account: Account

    @State private var hovering = false

    private var isPending: Bool { state.pendingSwitchID == account.id }

    var body: some View {
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
                        if account.usageStale {
                            Text("usage data is stale")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
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
