import SwiftUI

enum PopoverTab: String, CaseIterable, Identifiable {
    case accounts
    case usage

    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .accounts: "person.2"
        case .usage: "chart.bar.xaxis"
        }
    }
}

struct PopoverView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.colorScheme) private var colorScheme
    @State private var selectedTab: PopoverTab = .accounts

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if state.showingMachines {
                MachinesView()
            } else {
                header
                tabPicker
                    .padding(.horizontal, 4)
                    .padding(.bottom, 2)

                Divider().padding(.bottom, 2)

                Group {
                    switch selectedTab {
                    case .accounts:
                        accountsTab
                    case .usage:
                        usageTab
                    }
                }
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }

            if let error = state.errorMessage {
                Label(error, systemImage: "exclamationmark.octagon.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(3)
                    .padding(.horizontal, 8)
                    .onTapGesture { state.errorMessage = nil }
                    .help("Click to dismiss")
            }

            Divider().padding(.vertical, 2)
            footer
        }
        .padding(10)
        .frame(width: 360)
    }

    private var tabPicker: some View {
        Picker("Dashboard section", selection: $selectedTab) {
            ForEach(PopoverTab.allCases) { tab in
                Label(tab.title, systemImage: tab.symbol)
                    .tag(tab)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .accessibilityLabel("CCC dashboard section")
    }

    /// Accounts grouped by machine: this Mac first, then SSH hosts.
    @ViewBuilder
    private var accountsTab: some View {
        if !state.hasLoaded && state.errorMessage == nil {
            ProgressView("loading accounts…")
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 20)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    if AppState.proxyConfigured {
                        ProxyDashboard()
                        directLoginsToggle
                    }
                    if AppState.proxyConfigured && !state.showingDirectLogins {
                        EmptyView()
                    } else if let view = state.view {
                        accountList(view)
                    } else {
                        // ccc < 0.19: no account view, show machine groups.
                        ForEach(state.visibleMachines) { machine in
                            MachineSection(machine: machine)
                        }
                    }
                    if !state.hiddenDuplicates.isEmpty {
                        Text(duplicatesCaption)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .padding(.horizontal, 8)
                            .padding(.top, 2)
                    }
                }
                .padding(.trailing, 2)
            }
            .frame(maxHeight: 540)
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The account matrix: machine column headers once, then one line per
    /// account (Claude, then Codex) with a marker under each machine.
    @ViewBuilder
    private func accountList(_ view: AccountsView) -> some View {
        let layout = MatrixLayout(machines: view.machines.count)
        loginButton
        MatrixHeader(machines: view.machines, layout: layout)
        ForEach(["claude", "codex"], id: \.self) { provider in
            let rows = view.accounts.filter { $0.provider == provider }
            if !rows.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    Text(provider.uppercased())
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, MatrixLayout.rowInset)
                    ForEach(rows) { account in
                        AccountCard(account: account, machines: view.machines, layout: layout)
                    }
                }
            }
        }
        if view.accounts.isEmpty {
            Text("No accounts yet — log in with the button above.")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 8)
        }
    }

    /// Proxy mode: the per-machine logins (accounts the proxy cannot use, and
    /// the fallback when the proxy is down) stay one click away.
    private var directLoginsToggle: some View {
        Button {
            state.showingDirectLogins.toggle()
            if state.showingDirectLogins { Task { await state.refresh() } }
        } label: {
            Label(state.showingDirectLogins ? "Hide the machines' own logins" : "Show the machines' own logins",
                  systemImage: state.showingDirectLogins ? "chevron.down" : "chevron.right")
        }
        .buttonStyle(.plain)
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 8)
        .padding(.top, 2)
        .help("Logins that are not in the proxy: accounts it cannot use, and each machine's fallback.")
    }

    /// One button for the whole flow: sign in on this Mac, shared everywhere.
    private var loginButton: some View {
        Button {
            state.logInNewAccount()
        } label: {
            Label("Log in with new account", systemImage: "person.crop.circle.badge.plus")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.regular)
        .padding(.horizontal, 8)
        .padding(.bottom, 2)
        .help(state.syncEnabled
              ? "Opens Terminal: sign in once on this Mac; the hub shares it with every machine"
              : "Opens Terminal: sign in and store the account on this Mac")
    }

    private var duplicatesCaption: String {
        state.hiddenDuplicates.map { dup in
            "\(dup.displayName) is the same machine as \(state.twinName(dup.duplicateOf ?? ""))"
        }.joined(separator: " · ") + " — hidden."
    }

    private var usageTab: some View {
        WeeklyCostCard()
    }

    @ViewBuilder
    private var header: some View {
        let name = colorScheme == .dark ? "ccc-banner-dark" : "ccc-banner-light"
        if let url = Bundle.main.url(forResource: name, withExtension: "png"),
           let image = NSImage(contentsOf: url) {
            Image(nsImage: image)
                .resizable()
                .scaledToFit()
                .frame(height: 104)
                .frame(maxWidth: .infinity)
                .accessibilityHidden(true)
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Button {
                Task { await state.refresh() }
            } label: {
                if state.isRefreshing {
                    HStack(spacing: 4) {
                        ProgressView().controlSize(.mini)
                        Text(state.loadingMachines.count > 1
                             ? "refreshing \(state.loadingMachines.count) machines…" : "refreshing…")
                    }
                } else {
                    Label(refreshedCaption, systemImage: "arrow.clockwise")
                }
            }
            .buttonStyle(.plain)
            .font(.caption)
            .foregroundStyle(.secondary)
            .disabled(state.isRefreshing)

            Spacer()

            if oldControls {
            Toggle("auto-switch", isOn: Binding(
                get: { state.autoSwitchOn },
                set: { on in Task { await state.setAutoSwitch(on) } }
            ))
            .toggleStyle(.checkbox)
            .font(.caption)
            .foregroundStyle(.secondary)
            .disabled(state.autoSwitch == nil || state.autoSwitchBusy)
            .help("Switch Claude accounts on every machine when the active one has "
                  + "\(state.autoSwitchPercentLeft.usage)% or less left (5h or weekly), "
                  + "or \(state.autoSwitchPercentLeft.fable)% or less Fable left. Details: Machines.")
            }

            Toggle("start at login", isOn: Binding(
                get: { state.launchAtLogin },
                set: { state.setLaunchAtLogin($0) }
            ))
            .toggleStyle(.checkbox)
            .font(.caption)
            .foregroundStyle(.secondary)

            if oldControls || state.showingMachines {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { state.showingMachines.toggle() }
            } label: {
                Image(systemName: state.showingMachines ? "person.2" : "server.rack")
            }
            .buttonStyle(.plain)
            .foregroundStyle(state.showingMachines ? Color.accentColor : .secondary)
            .help(state.showingMachines ? "Back to accounts" : "Machines")
            .accessibilityLabel(state.showingMachines ? "Back to accounts" : "Machines")
            }

            Button {
                NSApplication.shared.terminate(nil)
            } label: {
                Image(systemName: "power")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Quit CCC")
        }
        .padding(.horizontal, 8)
        .padding(.bottom, 2)
    }

    /// Auto-switch and the machine manager belong to the per-machine logins.
    private var oldControls: Bool { !AppState.proxyConfigured || state.showingDirectLogins }

    private var refreshedCaption: String {
        guard let last = state.lastRefreshed else { return "refresh" }
        let seconds = Int(Date().timeIntervalSince(last))
        if seconds < 90 { return "updated \(seconds)s ago" }
        return "updated \(seconds / 60)m ago"
    }
}
