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

            if let error = state.errorMessage {
                Label(error, systemImage: "exclamationmark.octagon.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(3)
                    .padding(.horizontal, 8)
            }

            Divider().padding(.vertical, 2)
            footer
        }
        .padding(10)
        .frame(width: 340)
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

    @ViewBuilder
    private var accountsTab: some View {
        if let snapshot = state.snapshot {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(snapshot.providers) { provider in
                    section(for: provider)
                }
            }
        } else if state.errorMessage == nil {
            ProgressView("loading accounts…")
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 20)
        }
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

    private func section(for provider: Provider) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(provider.title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 8)
            if provider.accounts.isEmpty {
                Text(provider.available ? "no accounts found" : "provider unavailable")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 8)
                    .padding(.bottom, 4)
            } else {
                ForEach(provider.accounts) { account in
                    AccountRow(provider: provider, account: account)
                }
            }
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
                        Text("refreshing…")
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
