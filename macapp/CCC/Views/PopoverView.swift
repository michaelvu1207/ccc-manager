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
                    if let view = state.view {
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

    /// One row per account (Claude, then Codex), each with a chip per machine.
    @ViewBuilder
    private func accountList(_ view: AccountsView) -> some View {
        machineStrip(view.machines)
        ForEach(["claude", "codex"], id: \.self) { provider in
            let rows = view.accounts.filter { $0.provider == provider }
            if !rows.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    Text(provider.uppercased())
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 8)
                    ForEach(rows) { account in
                        AccountCard(account: account, machines: view.machines)
                    }
                }
            }
        }
        if view.accounts.isEmpty {
            Text("No accounts yet — add one from a machine below.")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 8)
        }
    }

    /// Machine status line: loading / offline at a glance, plus "add account".
    private func machineStrip(_ machines: [ViewMachine]) -> some View {
        FlowLayout(spacing: 8, lineSpacing: 4) {
            ForEach(machines) { machine in
                Menu {
                    Text(machine.name + (machine.online ? "" : " — offline"))
                    Button("Add a Claude account here…") { state.addAccount(machineID: machine.id) }
                        .disabled(!machine.online)
                    Button("Refresh") { Task { await state.refresh(machineID: machine.id) } }
                } label: {
                    HStack(spacing: 3) {
                        if state.loadingMachines.contains(machine.id) {
                            ProgressView().controlSize(.mini).scaleEffect(0.6).frame(width: 8, height: 8)
                        } else {
                            Circle()
                                .fill(machine.online ? Color.green : Color.red)
                                .frame(width: 5, height: 5)
                        }
                        Image(systemName: platformSymbol(machine.platform, local: machine.local))
                            .font(.system(size: 9))
                        Text(machine.shortName)
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("\(machine.name) — \(machine.online ? "online" : "offline")")
            }
        }
        .padding(.horizontal, 8)
        .padding(.bottom, 2)
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

            Toggle("start at login", isOn: Binding(
                get: { state.launchAtLogin },
                set: { state.setLaunchAtLogin($0) }
            ))
            .toggleStyle(.checkbox)
            .font(.caption)
            .foregroundStyle(.secondary)

            Button {
                withAnimation(.easeInOut(duration: 0.15)) { state.showingMachines.toggle() }
            } label: {
                Image(systemName: state.showingMachines ? "person.2" : "server.rack")
            }
            .buttonStyle(.plain)
            .foregroundStyle(state.showingMachines ? Color.accentColor : .secondary)
            .help(state.showingMachines ? "Back to accounts" : "Machines")
            .accessibilityLabel(state.showingMachines ? "Back to accounts" : "Machines")

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

    private var refreshedCaption: String {
        guard let last = state.lastRefreshed else { return "refresh" }
        let seconds = Int(Date().timeIntervalSince(last))
        if seconds < 90 { return "updated \(seconds)s ago" }
        return "updated \(seconds / 60)m ago"
    }
}
