import SwiftUI

/// `ccc proxy status --json`: the cap guard's last reading of every account
/// in the proxy, plus this Mac's own setup (`client`).
struct ProxyStatus: Decodable {
    struct Window: Decodable {
        let kind: String        // week | short | model
        let name: String
        let pct: Double
        let resetsAt: Double?

        enum CodingKeys: String, CodingKey {
            case kind, name, pct
            case resetsAt = "resets_at"
        }

        var left: Double { min(100, max(0, 100 - pct)) }

        /// `wk` / `5h` / `fable`: the short labels the usage bars use.
        var label: String {
            switch kind {
            case "week": "wk"
            case "short": "5h"
            default: String(name.split(separator: " ").first ?? "").lowercased()
            }
        }

        func usageWindow(now: Date = Date()) -> UsageWindow {
            UsageWindow(id: label, pctUsed: pct, resetsIn: ProxyStatus.countdown(to: resetsAt, now: now))
        }
    }

    struct Account: Decodable, Identifiable {
        let provider: String
        let email: String
        let windows: [Window]?
        let disabled: Bool?
        let disabledByGuard: Bool?
        let excludedModels: [String]?
        let reason: String?
        let error: String?

        enum CodingKeys: String, CodingKey {
            case provider, email, windows, disabled, reason, error
            case disabledByGuard = "disabled_by_guard"
            case excludedModels = "excluded_models"
        }

        var id: String { "\(provider)|\(email)" }

        enum State: Equatable { case on, limited, capped, offByHand, needsLogin, unknown }

        var state: State {
            if error?.contains("HTTP 401") == true || error?.contains("HTTP 403") == true { return .needsLogin }
            if disabled == true { return disabledByGuard == true ? .capped : .offByHand }
            if error != nil { return .unknown }
            if let models = excludedModels, !models.isEmpty { return .limited }
            return .on
        }

        /// The proxy gives this account requests now.
        var takesRequests: Bool { state == .on || state == .limited || state == .unknown }

        var week: Window? { windows?.first { $0.kind == "week" } }
    }

    struct Client: Decodable {
        let host: String?
        let claude: String?
        let codex: Bool?
    }

    let at: Double?
    let accounts: [String: Account]?
    let client: Client?

    func rows(_ provider: String) -> [Account] {
        (accounts ?? [:]).values.filter { $0.provider == provider }.sorted { $0.email < $1.email }
    }

    /// Seconds since the guard's reading; nil when there is none.
    func age(now: Date = Date()) -> TimeInterval? {
        at.map { now.timeIntervalSince1970 - $0 }
    }

    /// Mean week left of the accounts that take requests, 0...100.
    func capacity(_ provider: String) -> Double? {
        let lefts = rows(provider).filter(\.takesRequests).compactMap { $0.week?.left }
        return lefts.isEmpty ? nil : lefts.reduce(0, +) / Double(lefts.count)
    }

    static func countdown(to epoch: Double?, now: Date = Date()) -> String {
        guard let epoch else { return "" }
        let seconds = Int(epoch - now.timeIntervalSince1970)
        if seconds <= 0 { return "now" }
        if seconds >= 86400 { return "\(seconds / 86400)d \(seconds % 86400 / 3600)h" }
        if seconds >= 3600 { return "\(seconds / 3600)h \(seconds % 3600 / 60)m" }
        return "\(max(1, seconds / 60))m"
    }
}

/// The Accounts tab in proxy mode: the proxy owns every login, so this is the
/// whole account UX. Capacity for each provider, one card for each account,
/// one button to add a login, and this Mac's own switch.
struct ProxyDashboard: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let proxy = state.proxy {
                summary(proxy)
                health(proxy)
                addMenu
                ForEach(["claude", "codex"], id: \.self) { provider in
                    let rows = proxy.rows(provider)
                    if !rows.isEmpty {
                        Text(provider.uppercased())
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 8)
                        ForEach(rows) { card($0) }
                    }
                }
                if (proxy.accounts ?? [:]).isEmpty {
                    Text("No reading yet. The cap guard on the proxy host has not run.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 8)
                }
            } else if let error = state.proxyError {
                Label("Cannot read the proxy: \(error)", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 8)
            } else {
                ProgressView("reading the proxy…")
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 20)
            }
            if state.proxy == nil { addMenu }
            thisMac
        }
    }

    // MARK: - Summary

    private func summary(_ proxy: ProxyStatus) -> some View {
        HStack(spacing: 8) {
            ForEach(["claude", "codex"], id: \.self) { provider in
                let rows = proxy.rows(provider)
                let on = rows.filter(\.takesRequests).count
                VStack(alignment: .leading, spacing: 3) {
                    Text(provider.capitalized)
                        .font(.caption.weight(.semibold))
                    Text("\(on) of \(rows.count) in use")
                        .font(.caption2)
                        .foregroundStyle(on == 0 ? .red : .secondary)
                    if let left = proxy.capacity(provider) {
                        Text("\(Int(left))% week left")
                            .font(.caption2.monospaced())
                            .foregroundStyle(UsageBar.color(forLeft: left))
                    } else {
                        Text(on == 0 ? "no account available" : "usage unknown")
                            .font(.caption2)
                            .foregroundStyle(on == 0 ? .red : .secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(.horizontal, 8)
    }

    @ViewBuilder
    private func health(_ proxy: ProxyStatus) -> some View {
        let host = proxy.client?.host ?? "this Mac"
        if let age = proxy.age(), age > 600 {
            Label("The cap guard on \(host) last read \(Int(age / 60)) min ago. Caps are not checked.",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundStyle(.orange)
                .padding(.horizontal, 8)
        } else {
            Text("Proxy on \(host) · one session stays on one account · caps checked every 2 min")
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 8)
        }
    }

    // MARK: - Account card

    private func card(_ account: ProxyStatus.Account) -> some View {
        let busy = state.pendingProxyAccount == account.id
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(account.email)
                    .font(.callout.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundStyle(account.takesRequests ? .primary : .secondary)
                Spacer(minLength: 4)
                if busy {
                    ProgressView().controlSize(.mini)
                } else {
                    chip(account)
                }
                Menu {
                    if account.disabled == true {
                        Button("Turn on") { state.setProxyAccount(account, on: true) }
                    } else {
                        Button("Turn off") { state.setProxyAccount(account, on: false) }
                    }
                    Button("Log in again…") { state.logInToProxy(provider: account.provider) }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .disabled(busy)
            }
            ForEach(Array((account.windows ?? []).enumerated()), id: \.offset) { _, window in
                UsageBar(window: window.usageWindow())
            }
            if account.state == .needsLogin {
                Button {
                    state.logInToProxy(provider: account.provider)
                } label: {
                    Label("Log in again", systemImage: "person.badge.key")
                }
                .controlSize(.small)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
    }

    private func chip(_ account: ProxyStatus.Account) -> some View {
        let (text, color): (String, Color) = switch account.state {
        case .on: ("in use", .green)
        case .limited: ("in use, no Fable", .orange)
        case .capped: ("at cap" + resetSuffix(account), .orange)
        case .offByHand: ("off by hand", .secondary)
        case .needsLogin: ("needs login", .red)
        case .unknown: ("usage unknown", .secondary)
        }
        return Text(text)
            .font(.caption2.weight(.medium))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
            .help(account.reason ?? account.error ?? "")
    }

    private func resetSuffix(_ account: ProxyStatus.Account) -> String {
        let text = ProxyStatus.countdown(to: account.week?.resetsAt)
        return text.isEmpty ? "" : " · back in \(text)"
    }

    // MARK: - Actions

    private var addMenu: some View {
        Menu {
            Button("Claude account") { state.logInToProxy(provider: "claude") }
            Button("Codex account") { state.logInToProxy(provider: "codex") }
        } label: {
            Label("Add account", systemImage: "person.crop.circle.badge.plus")
                .frame(maxWidth: .infinity)
        }
        .menuStyle(.borderedButton)
        .controlSize(.regular)
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 8)
        .help("Opens Terminal: sign in in this Mac's browser. The login is saved in the proxy, "
              + "so every machine can use the account immediately.")
    }

    private var thisMac: some View {
        let pointed = state.proxy?.client?.claude != nil || state.proxy?.client?.codex == true
        return Toggle(isOn: Binding(
            get: { pointed },
            set: { on in Task { await state.setThisMacUsesProxy(on) } }
        )) {
            VStack(alignment: .leading, spacing: 1) {
                Text("Claude Code and Codex on this Mac use the proxy")
                    .font(.caption)
                Text(pointed ? "Off: this Mac uses its own login (claude.ai connectors and /usage work again)."
                             : "This Mac uses its own login now.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .toggleStyle(.switch)
        .controlSize(.mini)
        .disabled(state.proxy == nil || state.proxyPointBusy)
        .padding(.horizontal, 8)
    }
}
