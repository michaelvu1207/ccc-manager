import SwiftUI

/// Platform glyph shared by the popover and the Machines screen.
func platformSymbol(_ platform: String, local: Bool) -> String {
    switch platform {
    case "Darwin": return local ? "laptopcomputer" : "desktopcomputer"
    case "Linux": return "server.rack"
    default: return "questionmark.square.dashed"
    }
}

/// Status dot: green online, red offline, grey while nothing is known yet.
struct StatusDot: View {
    let machine: MachineSnapshot

    private var color: Color {
        if !machine.hasData && machine.error.isEmpty { return .secondary.opacity(0.5) }
        return machine.online ? .green : .red
    }

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 6, height: 6)
    }
}

/// One machine: a collapsible header plus its Claude and Codex accounts.
struct MachineSection: View {
    @EnvironmentObject private var state: AppState
    let machine: MachineSnapshot

    @State private var hoveringHeader = false

    private var collapsed: Bool { state.isCollapsed(machine) }
    private var loading: Bool { state.isLoading(machine) }
    private var offline: Bool { machine.hasData && !machine.online }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            header
            if !collapsed {
                content
                    .padding(.leading, 10)
                    .transition(.opacity)
            }
        }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 6) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { state.toggleCollapsed(machine) }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(collapsed ? 0 : 90))
                        .frame(width: 10)
                    Image(systemName: platformSymbol(machine.platform, local: machine.local))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .frame(width: 16)
                    Text(machine.displayName)
                        .font(.callout.weight(.semibold))
                        .lineLimit(1)
                    if machine.local {
                        Text("this Mac")
                            .font(.caption2)
                            .padding(.horizontal, 4)
                            .padding(.vertical, 1)
                            .background(.quaternary, in: Capsule())
                            .foregroundStyle(.secondary)
                    }
                    if loading {
                        ProgressView().controlSize(.mini)
                    } else {
                        StatusDot(machine: machine)
                    }
                    Spacer(minLength: 4)
                    if collapsed { collapsedSummary }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(statusHelp)

            Button {
                state.addAccount(machine: machine)
            } label: {
                Image(systemName: "plus")
                    .font(.system(size: 10, weight: .semibold))
                    .frame(width: 18, height: 18)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .opacity(hoveringHeader ? 1 : 0.0)
            .allowsHitTesting(hoveringHeader)
            .disabled(offline)
            .help("Add a Claude account on \(machine.displayName) (opens Terminal)")
        }
        .padding(.vertical, 4)
        .padding(.horizontal, 6)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(hoveringHeader ? Color.primary.opacity(0.05) : .clear)
        )
        .onHover { hoveringHeader = $0 }
    }

    /// When collapsed: the active Claude account's 5h fuel, or the status.
    @ViewBuilder
    private var collapsedSummary: some View {
        if offline {
            Text("offline").font(.caption2).foregroundStyle(.tertiary)
        } else if let active = machine.provider("claude")?.activeAccount {
            if active.needsLogin {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            } else if let left = active.window("5h")?.pctLeft {
                Text("5h \(Int(left))%")
                    .font(.caption2.monospaced())
                    .foregroundStyle(UsageBar.color(forLeft: left))
            }
        } else {
            Text("\(machine.accountCount) accounts")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
    }

    private var statusHelp: String {
        if loading { return "Refreshing \(machine.displayName)…" }
        if !machine.hasData { return machine.error.isEmpty ? "Not reached yet" : machine.error }
        if !machine.online { return machine.error.isEmpty ? "Offline" : machine.error }
        if let alias = machine.alias { return "Online · ssh \(alias)" }
        return "This Mac"
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        if offline || (!machine.hasData && !machine.error.isEmpty) {
            Label(offlineCaption, systemImage: "wifi.slash")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .padding(.horizontal, 8)
                .padding(.bottom, 2)
        } else if machine.online && !machine.error.isEmpty {
            Label(machine.error, systemImage: "exclamationmark.circle")
                .font(.caption2)
                .foregroundStyle(.orange)
                .lineLimit(2)
                .padding(.horizontal, 8)
        }

        if !machine.hasData {
            if loading || machine.error.isEmpty {
                Text("connecting…")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 8)
                    .padding(.bottom, 4)
            }
        } else {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(machine.providers.filter(showsProvider)) { provider in
                    providerSection(provider)
                }
            }
            .opacity(offline ? 0.55 : 1)
            .disabled(offline)
        }
    }

    /// Codex only appears where codex-auth is installed or has accounts.
    private func showsProvider(_ provider: Provider) -> Bool {
        provider.id == "claude" || !provider.accounts.isEmpty
    }

    private var offlineCaption: String {
        var text = "offline"
        if let fetched = machine.fetchedAt, machine.hasData {
            text += " · last seen \(Self.age(since: fetched)) ago"
        }
        let reason = machine.error.replacingOccurrences(of: "offline: ", with: "")
        if !reason.isEmpty && reason != "offline" { text += " · \(reason)" }
        return text
    }

    static func age(since epoch: Double) -> String {
        let seconds = max(0, Int(Date().timeIntervalSince1970 - epoch))
        if seconds < 90 { return "\(seconds)s" }
        if seconds < 5400 { return "\(seconds / 60)m" }
        if seconds < 129_600 { return "\(seconds / 3600)h" }
        return "\(seconds / 86400)d"
    }

    private func providerSection(_ provider: Provider) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(provider.title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 8)
                .padding(.top, 2)
            if provider.accounts.isEmpty {
                emptyProvider(provider)
            } else {
                ForEach(provider.accounts) { account in
                    AccountRow(machine: machine, provider: provider, account: account)
                }
            }
        }
    }

    @ViewBuilder
    private func emptyProvider(_ provider: Provider) -> some View {
        if let error = provider.error, !error.isEmpty {
            Text(error)
                .font(.caption)
                .foregroundStyle(.orange)
                .padding(.horizontal, 8)
                .padding(.bottom, 4)
        } else if !provider.available {
            Text(machine.local ? "provider unavailable" : "claude-swap not installed")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 8)
                .padding(.bottom, 4)
        } else {
            Button {
                state.addAccount(machine: machine)
            } label: {
                Label("No accounts yet — add one", systemImage: "plus.circle")
                    .font(.caption)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.accentColor)
            .padding(.horizontal, 8)
            .padding(.bottom, 4)
            .help("Opens Terminal: log in with claude, then cswap add")
        }
    }
}
