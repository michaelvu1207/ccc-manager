import Foundation

struct WeeklyCostViewModel {
    enum ContentState: Equatable {
        case loading
        case empty
        case content
        case error(String)
    }

    struct ChartPoint: Identifiable, Equatable {
        let date: String
        let provider: String
        let cost: Double
        let status: String

        var id: String { date + "|" + provider }
    }

    let report: CostReport?
    let hasSnapshot: Bool
    let isRefreshing: Bool
    let errorMessage: String?
    let now: Date

    init(
        snapshot: Snapshot?, isRefreshing: Bool, errorMessage: String?, now: Date = Date()
    ) {
        report = snapshot?.costs
        hasSnapshot = snapshot != nil
        self.isRefreshing = isRefreshing
        self.errorMessage = errorMessage
        self.now = now
    }

    var state: ContentState {
        guard let report else {
            if !hasSnapshot || isRefreshing {
                return errorMessage.map(ContentState.error) ?? .loading
            }
            return .error(errorMessage ?? "Seven-day costs are unavailable; update the CCC command-line tool")
        }
        return report.days.allSatisfy { $0.eventCount == 0 } ? .empty : .content
    }

    var chartPoints: [ChartPoint] {
        guard let report else { return [] }
        return report.days.flatMap { day in
            providerOrder.compactMap { provider -> ChartPoint? in
                guard let bucket = day.providers[provider], bucket.eventCount > 0,
                      let value = bucket.chartCost else {
                    return nil
                }
                return ChartPoint(
                    date: day.date, provider: provider, cost: value, status: bucket.status
                )
            }
        }
    }

    var orderedDays: [String] { report?.days.map(\.date) ?? [] }

    var today: DailyCost? {
        guard let report else { return nil }
        return report.days.first { $0.date == localDateString(timezone: report.timezone) }
    }

    var weeklyCostText: String {
        guard let report else { return "—" }
        return format(bucket: CostBucket(
            cost: report.cost, knownCost: report.knownCost, status: report.status,
            tokens: report.tokens, eventCount: report.eventCount
        ))
    }

    var todayCostText: String {
        guard let today else { return "$0.00" }
        return format(bucket: today.bucket)
    }

    var providerOrder: [String] {
        let preferred = ["claude", "codex"]
        guard let report else { return preferred }
        return preferred + report.providers.keys.filter { !preferred.contains($0) }.sorted()
    }

    var accessibilitySummary: String {
        guard let report else { return "Seven-day cost is loading" }
        let providers = providerOrder.compactMap { provider -> String? in
            guard let bucket = report.providers[provider] else { return nil }
            return "\(providerName(provider)) \(format(bucket: bucket))"
        }.joined(separator: ", ")
        let qualifier = qualityLabel(report.status)
        let days = report.days.map {
            "\(weekday(for: $0.date)) \(format(bucket: $0.bucket))"
        }.joined(separator: ", ")
        return "Past seven days \(weeklyCostText), today \(todayCostText). \(providers). Daily: \(days). \(qualifier)"
    }

    func weekday(for date: String) -> String {
        guard let report,
              let parsed = Self.dateFormatter(timezone: report.timezone).date(from: date)
        else { return date }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: report.timezone) ?? .current
        formatter.dateFormat = "EEEEE"
        return formatter.string(from: parsed)
    }

    func format(bucket: CostBucket) -> String {
        switch bucket.status {
        case "partial":
            return bucket.knownCost > 0 ? "at least \(currency(bucket.knownCost))" : "—"
        case "unknown":
            return "—"
        case "estimated":
            return bucket.cost.map { "about \(currency($0))" } ?? "—"
        default:
            return bucket.cost.map(currency) ?? "—"
        }
    }

    func qualityLabel(_ status: String) -> String {
        switch status {
        case "estimated": return "Estimated from token pricing"
        case "partial": return "Partial cost; the shown amount is a known lower bound"
        case "unknown": return "Cost unavailable because pricing is missing"
        default: return "Exact reported cost"
        }
    }

    func providerName(_ provider: String) -> String {
        switch provider {
        case "claude": return "Claude"
        case "codex": return "Codex"
        default: return provider.capitalized
        }
    }

    func tokens(_ count: Int) -> String {
        count.formatted(.number.notation(.compactName))
    }

    func tokenBreakdown(_ counts: TokenCounts) -> String {
        var parts = ["\(tokens(counts.total)) tokens"]
        if counts.input > 0 { parts.append("\(tokens(counts.input)) in") }
        if counts.output > 0 { parts.append("\(tokens(counts.output)) out") }
        if counts.reasoning > 0 { parts.append("\(tokens(counts.reasoning)) reasoning") }
        return parts.joined(separator: " · ")
    }

    private func currency(_ value: Double) -> String {
        value.formatted(.currency(code: "USD").precision(.fractionLength(2)))
    }

    private func localDateString(timezone: String) -> String {
        Self.dateFormatter(timezone: timezone).string(from: now)
    }

    private static func dateFormatter(timezone: String) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: timezone) ?? .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }
}
