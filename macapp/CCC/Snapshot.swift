import Foundation

/// Mirrors the JSON emitted by `ccc snapshot` (schema version 1).
struct Snapshot: Codable {
    let version: Int
    let generatedAt: Double
    let providers: [Provider]

    enum CodingKeys: String, CodingKey {
        case version
        case generatedAt = "generated_at"
        case providers
    }

    func provider(_ id: String) -> Provider? {
        providers.first { $0.id == id }
    }
}

struct Provider: Codable, Identifiable {
    let id: String  // "claude" | "codex"
    let available: Bool
    let accounts: [Account]

    var title: String { id.uppercased() }
    var activeAccount: Account? { accounts.first { $0.active } }
}

struct Account: Codable, Identifiable {
    let num: String
    let email: String
    let label: String
    let plan: String
    let active: Bool
    let needsLogin: Bool
    let windows: [UsageWindow]
    let usageStale: Bool

    var id: String { num + "|" + email }

    enum CodingKeys: String, CodingKey {
        case num, email, label, plan, active, windows
        case needsLogin = "needs_login"
        case usageStale = "usage_stale"
    }

    func window(_ id: String) -> UsageWindow? {
        windows.first { $0.id == id }
    }
}

struct UsageWindow: Codable, Identifiable {
    let id: String  // "5h" | "wk"
    let pctUsed: Double?
    let resetsIn: String

    enum CodingKeys: String, CodingKey {
        case id
        case pctUsed = "pct_used"
        case resetsIn = "resets_in"
    }

    /// Percent remaining, clamped to 0...100 — the fuel-gauge number the TUI shows.
    var pctLeft: Double? {
        pctUsed.map { min(100, max(0, 100 - $0)) }
    }
}
