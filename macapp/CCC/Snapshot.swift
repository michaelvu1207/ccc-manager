import Foundation

/// Mirrors the JSON emitted by `ccc snapshot` (schema version 2, v1-compatible).
struct Snapshot: Codable {
    let version: Int
    let generatedAt: Double
    let providers: [Provider]
    /// Additive in snapshot schema v2. Older v1 snapshots continue to decode.
    let costs: CostReport?
    /// Additive (0.18): this Mac plus SSH machines. Absent from older ccc builds.
    let machines: [MachineSnapshot]?

    enum CodingKeys: String, CodingKey {
        case version
        case generatedAt = "generated_at"
        case providers
        case costs
        case machines
    }

    /// Machines to show; older snapshots present their providers as this Mac.
    var machineList: [MachineSnapshot] {
        machines ?? [MachineSnapshot.localFallback(providers: providers)]
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
    /// Why a remote provider has no accounts (e.g. "cswap list failed").
    var error: String? = nil

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
    /// Additive (0.18): held out of auto-rotation; alias; raw usage status.
    var disabled: Bool? = nil
    var alias: String? = nil
    var status: String? = nil

    var id: String { num + "|" + email }
    var isDisabled: Bool { disabled ?? false }

    enum CodingKeys: String, CodingKey {
        case num, email, label, plan, active, windows, disabled, alias, status
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

// MARK: - Machines (0.18)

/// One machine's accounts, as seen by its last load (this Mac) or probe (SSH).
struct MachineSnapshot: Codable, Identifiable {
    let id: String
    let alias: String?
    let displayName: String
    let local: Bool
    let engine: String?
    let platform: String  // `uname -s`: "Darwin" | "Linux" | "" (never reached)
    let online: Bool
    let error: String
    let fetchedAt: Double?
    let stale: Bool
    let hasData: Bool
    let duplicateOf: String?
    let providers: [Provider]

    enum CodingKeys: String, CodingKey {
        case id, alias, local, engine, platform, online, error, stale, providers
        case displayName = "display_name"
        case fetchedAt = "fetched_at"
        case hasData = "has_data"
        case duplicateOf = "duplicate_of"
    }

    static func localFallback(providers: [Provider]) -> MachineSnapshot {
        MachineSnapshot(
            id: "local", alias: nil, displayName: "This Mac", local: true, engine: "ccc",
            platform: "Darwin", online: true, error: "", fetchedAt: nil, stale: false,
            hasData: true, duplicateOf: nil, providers: providers
        )
    }

    func provider(_ id: String) -> Provider? {
        providers.first { $0.id == id }
    }

    var accountCount: Int { providers.reduce(0) { $0 + $1.accounts.count } }
}

/// One entry of `ccc machines list --json` (the editable machine list).
struct MachineConfig: Codable, Identifiable, Equatable {
    let id: String
    let name: String
    let alias: String?
    let local: Bool
    let enabled: Bool
    let engine: String?
}

struct MachineList: Codable {
    let configPath: String
    let customized: Bool
    let machines: [MachineConfig]

    enum CodingKeys: String, CodingKey {
        case machines, customized
        case configPath = "config_path"
    }
}

/// Stable key for one account on one machine (pending / confirm UI state).
struct AccountKey: Hashable {
    let machine: String
    let provider: String
    let num: String
}
