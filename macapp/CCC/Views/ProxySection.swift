import SwiftUI

/// `ccc proxy status --json`: the cap guard's last reading of every account
/// in the proxy, plus this Mac's own setup (`client`).
struct ProxyStatus: Decodable {
    struct Window: Decodable {
        let kind: String        // week | short | model
        let name: String
        let pct: Double
    }

    struct Account: Decodable {
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

        /// A login the proxy can no longer use: sign in again.
        var needsLogin: Bool { error?.contains("HTTP 401") == true || error?.contains("HTTP 403") == true }

        var stateText: String {
            if needsLogin { return "needs login" }
            if disabled == true {
                return disabledByGuard == true ? "off: \(reason ?? "at its cap")" : "off (by hand)"
            }
            if let models = excludedModels, !models.isEmpty { return "on, no Fable" }
            return error == nil ? "on" : "usage unknown"
        }
    }

    struct Client: Decodable {
        let host: String?
        let claude: String?
        let codex: Bool?
    }

    let at: Double?
    let accounts: [String: Account]?
    let client: Client?

    var rows: [Account] {
        (accounts ?? [:]).values.sorted { ($0.provider, $0.email) < ($1.provider, $1.email) }
    }
}

/// Proxy mode in the Accounts tab: one login button, then one line per
/// account in the proxy with its week usage and state.
struct ProxySection: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            loginMenu
            if let proxy = state.proxy {
                Text(caption(proxy))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8)
                ForEach(Array(proxy.rows.enumerated()), id: \.offset) { _, account in
                    row(account)
                }
                if proxy.rows.isEmpty {
                    Text("No reading yet — the cap guard on the proxy host has not run.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 8)
                }
            } else if let error = state.proxyError {
                Text("Proxy: \(error)")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 8)
            }
            Divider().padding(.vertical, 2)
        }
    }

    private var loginMenu: some View {
        Menu {
            Button("Claude account") { state.logInToProxy(provider: "claude") }
            Button("Codex account") { state.logInToProxy(provider: "codex") }
        } label: {
            Label("Log in to proxy", systemImage: "person.crop.circle.badge.plus")
                .frame(maxWidth: .infinity)
        }
        .menuStyle(.borderedButton)
        .controlSize(.regular)
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 8)
        .help("Opens Terminal: sign in in this Mac's browser; the login is saved in the proxy for every machine. "
              + "Use it for a new account and to renew one that needs login.")
    }

    private func caption(_ proxy: ProxyStatus) -> String {
        let host = proxy.client?.host ?? "this Mac"
        let claude = proxy.client?.claude != nil ? "proxy" : "own login"
        let codex = proxy.client?.codex == true ? "proxy" : "own login"
        return "PROXY on \(host) · this Mac: Claude Code → \(claude), Codex → \(codex)"
    }

    private func row(_ account: ProxyStatus.Account) -> some View {
        let week = account.windows?.first { $0.kind == "week" }
        let off = account.disabled == true || account.needsLogin
        return HStack(spacing: 6) {
            Text(account.provider == "claude" ? "CL" : "CX")
                .font(.caption2.monospaced())
                .foregroundStyle(.secondary)
                .frame(width: 18, alignment: .leading)
            Text(account.email)
                .font(.caption)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let week {
                let left = max(0, 100 - week.pct)
                Capsule()
                    .fill(.quaternary)
                    .frame(width: 44, height: 5)
                    .overlay(alignment: .leading) {
                        Capsule().fill(UsageBar.color(forLeft: left)).frame(width: 44 * left / 100)
                    }
                Text("\(Int(left))% left")
                    .font(.caption2.monospaced())
                    .foregroundStyle(UsageBar.color(forLeft: left))
                    .frame(width: 58, alignment: .leading)
            }
            Text(account.stateText)
                .font(.caption2)
                .foregroundStyle(account.needsLogin ? .red : (off ? .orange : .secondary))
                .lineLimit(1)
                .frame(width: 92, alignment: .trailing)
            if account.needsLogin {
                Button("Re-login") { state.logInToProxy(provider: account.provider) }
                    .controlSize(.mini)
            }
        }
        .padding(.horizontal, 8)
        .help(account.reason ?? account.error ?? "")
    }
}
