import Foundation

/// Mirrors the JSON emitted by `ccc snapshot` (schema version 2, v1-compatible).
struct Snapshot: Codable {
    let version: Int
    let generatedAt: Double
    let providers: [Provider]
    /// Additive in snapshot schema v2. Older v1 snapshots continue to decode.
    let costs: CostReport?

    enum CodingKeys: String, CodingKey {
        case version
        case generatedAt = "generated_at"
        case providers
        case costs
    }

    func provider(_ id: String) -> Provider? {
        providers.first { $0.id == id }
    }
}

// MARK: - Rolling seven-day costs

struct CostReport: Codable {
    let schemaVersion: Int
    let generatedAt: String
    let timezone: String
    let startDate: String
    let endDate: String
    let status: String
    let cost: Double?
    let knownCost: Double
    let tokens: TokenCounts
    let eventCount: Int
    let pricingVersion: String
    let providers: [String: CostBucket]
    let accounts: [AccountCost]
    let days: [DailyCost]
    let models: [ModelCost]
    let deduplicatedEvents: Int
    let parseErrors: Int

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case timezone
        case startDate = "start_date"
        case endDate = "end_date"
        case status, cost
        case knownCost = "known_cost"
        case eventCount = "event_count"
        case pricingVersion = "pricing_version"
        case tokens, providers, accounts, days, models
        case deduplicatedEvents = "deduplicated_events"
        case parseErrors = "parse_errors"
    }
}

struct TokenCounts: Codable, Equatable {
    let input: Int
    let output: Int
    let cacheRead: Int
    let cacheWrite: Int
    let reasoning: Int
    let total: Int

    enum CodingKeys: String, CodingKey {
        case input, output, reasoning, total
        case cacheRead = "cache_read"
        case cacheWrite = "cache_write"
    }
}

struct CostBucket: Codable, Equatable {
    let cost: Double?
    let knownCost: Double
    let status: String
    let tokens: TokenCounts
    let eventCount: Int

    enum CodingKeys: String, CodingKey {
        case cost, status, tokens
        case knownCost = "known_cost"
        case eventCount = "event_count"
    }

    /// A partial bucket has a known lower bound, never a pretend exact total.
    var chartCost: Double? {
        cost ?? (status == "partial" && knownCost > 0 ? knownCost : nil)
    }
}

struct DailyCost: Codable, Identifiable {
    let date: String
    let cost: Double?
    let knownCost: Double
    let status: String
    let tokens: TokenCounts
    let eventCount: Int
    let providers: [String: CostBucket]

    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, cost, status, tokens, providers
        case knownCost = "known_cost"
        case eventCount = "event_count"
    }

    var bucket: CostBucket {
        CostBucket(
            cost: cost, knownCost: knownCost, status: status,
            tokens: tokens, eventCount: eventCount
        )
    }
}

struct AccountCost: Codable, Identifiable {
    let provider: String
    let account: String?
    let label: String?
    let cost: Double?
    let knownCost: Double
    let status: String
    let tokens: TokenCounts
    let eventCount: Int

    var id: String { provider + "|" + (account ?? label ?? "unattributed") }
    var displayName: String { label ?? account ?? "Unattributed" }

    enum CodingKeys: String, CodingKey {
        case provider, account, label, cost, status, tokens
        case knownCost = "known_cost"
        case eventCount = "event_count"
    }

    var bucket: CostBucket {
        CostBucket(
            cost: cost, knownCost: knownCost, status: status,
            tokens: tokens, eventCount: eventCount
        )
    }
}

struct ModelCost: Codable, Identifiable {
    let provider: String
    let model: String
    let cost: Double?
    let knownCost: Double
    let status: String
    let tokens: TokenCounts
    let eventCount: Int

    var id: String { provider + "|" + model }

    enum CodingKeys: String, CodingKey {
        case provider, model, cost, status, tokens
        case knownCost = "known_cost"
        case eventCount = "event_count"
    }

    var bucket: CostBucket {
        CostBucket(
            cost: cost, knownCost: knownCost, status: status,
            tokens: tokens, eventCount: eventCount
        )
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
    let statusMessage: String?
    let consumableResets: Int?
    let consumableResetCredits: [ResetCredit]?
    let windows: [UsageWindow]
    let usageStale: Bool

    var id: String { num + "|" + email }

    enum CodingKeys: String, CodingKey {
        case num, email, label, plan, active, windows
        case needsLogin = "needs_login"
        case statusMessage = "status_message"
        case consumableResets = "consumable_resets"
        case consumableResetCredits = "consumable_reset_credits"
        case usageStale = "usage_stale"
    }

    func window(_ id: String) -> UsageWindow? {
        windows.first { $0.id == id }
    }
}

struct ResetCredit: Codable, Identifiable, Equatable {
    let id: String
    let title: String
    let expiresAt: String?

    enum CodingKeys: String, CodingKey {
        case id, title
        case expiresAt = "expires_at"
    }

    var actionLabel: String {
        guard let expiresAt else {
            return title
        }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = fractional.date(from: expiresAt) ?? ISO8601DateFormatter().date(from: expiresAt)
        guard let date else { return title }
        return "\(title) — expires \(date.formatted(date: .abbreviated, time: .omitted))"
    }
}

struct UsageWindow: Codable, Identifiable {
    let id: String  // "5h" | "wk" | model-scoped caps like "fable"
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
