import XCTest
@testable import CCC

final class WeeklyCostViewModelTests: XCTestCase {
    func testPopoverHasSeparateAccountAndUsageTabs() {
        XCTAssertEqual(PopoverTab.allCases, [.accounts, .usage])
        XCTAssertEqual(PopoverTab.accounts.symbol, "person.2")
        XCTAssertEqual(PopoverTab.usage.symbol, "chart.bar.xaxis")
    }

    func testCodexAccountDecodesConsumableResetCount() throws {
        let account = try JSONDecoder().decode(Account.self, from: Data("""
        {
          "num":"1", "email":"pro@example.test", "label":"Pro", "plan":"Pro",
          "active":true, "needs_login":false, "status_message":"",
          "consumable_resets":3,
          "consumable_reset_credits":[
            {"id":"credit-1","title":"Launch reset","expires_at":"2026-08-01T00:00:00Z"}
          ],
          "windows":[], "usage_stale":false
        }
        """.utf8))

        XCTAssertEqual(account.consumableResets, 3)
        XCTAssertEqual(account.consumableResetCredits?.first?.id, "credit-1")
        XCTAssertTrue(account.consumableResetCredits?.first?.actionLabel.contains("Launch reset") == true)
        XCTAssertTrue(account.consumableResetCredits?.first?.actionLabel.contains("expires") == true)
    }

    func testLegacySnapshotWithoutCostsStillDecodes() throws {
        let snapshot = try decode("""
        {
          "version": 1,
          "generated_at": 1,
          "providers": []
        }
        """)

        XCTAssertNil(snapshot.costs)
        XCTAssertEqual(
            WeeklyCostViewModel(
                snapshot: snapshot, isRefreshing: false, errorMessage: nil
            ).state,
            .error("Seven-day costs are unavailable; update the CCC command-line tool")
        )
    }

    func testExactEmptyReportUsesEmptyState() throws {
        let snapshot = try decode(snapshotJSON(status: "exact", cost: "0", knownCost: 0, events: 0))
        let model = WeeklyCostViewModel(
            snapshot: snapshot, isRefreshing: false, errorMessage: nil
        )

        XCTAssertEqual(model.state, .empty)
        XCTAssertTrue(model.weeklyCostText.contains("0.00"))
        XCTAssertTrue(model.chartPoints.isEmpty)
    }

    func testEstimatedReportBuildsProviderDifferentiatedPoints() throws {
        let snapshot = try decode(
            snapshotJSON(status: "estimated", cost: "3.5", knownCost: 3.5, events: 2)
        )
        let model = WeeklyCostViewModel(
            snapshot: snapshot, isRefreshing: false, errorMessage: nil
        )

        XCTAssertEqual(model.state, .content)
        XCTAssertEqual(Set(model.chartPoints.map(\.provider)), ["claude", "codex"])
        XCTAssertTrue(model.weeklyCostText.contains("about"))
        XCTAssertTrue(model.accessibilitySummary.contains("Estimated"))
    }

    func testPartialCostShowsKnownLowerBoundInsteadOfZero() throws {
        let snapshot = try decode(
            snapshotJSON(status: "partial", cost: "null", knownCost: 1.25, events: 2)
        )
        let model = WeeklyCostViewModel(
            snapshot: snapshot, isRefreshing: false, errorMessage: nil
        )

        XCTAssertTrue(model.weeklyCostText.contains("at least"))
        XCTAssertTrue(model.weeklyCostText.contains("1.25"))
        XCTAssertTrue(model.accessibilitySummary.contains("lower bound"))
    }

    func testTodayUsesReportTimezoneNearUTCMidnight() throws {
        let snapshot = try decode(
            snapshotJSON(status: "estimated", cost: "3.5", knownCost: 3.5, events: 2)
        )
        let now = ISO8601DateFormatter().date(from: "2026-07-10T06:30:00Z")!
        let model = WeeklyCostViewModel(
            snapshot: snapshot, isRefreshing: false, errorMessage: nil, now: now
        )

        XCTAssertEqual(model.today?.date, "2026-07-09")
        XCTAssertTrue(model.todayCostText.contains("3.50"))
    }

    func testSevenCategoricalDaysRemainIntactAcrossDSTBoundary() throws {
        let json = snapshotJSON(
            status: "estimated", cost: "3.5", knownCost: 3.5, events: 2,
            dates: [
                "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-07",
                "2026-03-08", "2026-03-09", "2026-03-10",
            ]
        )
        let model = WeeklyCostViewModel(
            snapshot: try decode(json), isRefreshing: false, errorMessage: nil
        )

        XCTAssertEqual(model.orderedDays.count, 7)
        XCTAssertEqual(Set(model.orderedDays).count, 7)
        XCTAssertEqual(model.weekday(for: "2026-03-08"), "S")
    }

    private func decode(_ json: String) throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    private func snapshotJSON(
        status: String,
        cost: String,
        knownCost: Double,
        events: Int,
        dates: [String] = [
            "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06",
            "2026-07-07", "2026-07-08", "2026-07-09",
        ]
    ) -> String {
        let days = dates.enumerated().map { index, date in
            let isLast = index == dates.count - 1
            let eventCount = isLast ? events : 0
            let claudeCost = isLast && events > 0 ? 2.0 : 0.0
            let codexCost = isLast && events > 0 ? 1.0 : 0.0
            return """
            {
              "date":"\(date)", "cost":\(isLast ? cost : "0"),
              "known_cost":\(isLast ? knownCost : 0), "status":"\(isLast ? status : "exact")",
              "tokens":\(tokens(total: isLast ? 30 : 0)), "event_count":\(eventCount),
              "providers":{
                "claude":\(bucket(cost: claudeCost, events: eventCount > 0 ? 1 : 0)),
                "codex":\(bucket(cost: codexCost, events: eventCount > 1 ? 1 : 0))
              }
            }
            """
        }.joined(separator: ",")
        return """
        {
          "version":2, "generated_at":1, "providers":[],
          "costs":{
            "schema_version":1, "generated_at":"2026-07-09T20:00:00Z",
            "timezone":"America/Los_Angeles", "start_date":"\(dates.first!)",
            "end_date":"\(dates.last!)", "status":"\(status)", "cost":\(cost),
            "known_cost":\(knownCost), "tokens":\(tokens(total: events * 15)),
            "event_count":\(events), "pricing_version":"2026-07-09",
            "providers":{
              "claude":\(bucket(cost: 2, events: events > 0 ? 1 : 0)),
              "codex":\(bucket(cost: 1, events: events > 1 ? 1 : 0))
            },
            "accounts":[], "days":[\(days)], "models":[],
            "deduplicated_events":0, "parse_errors":0
          }
        }
        """
    }

    private func bucket(cost: Double, events: Int) -> String {
        """
        {"cost":\(cost),"known_cost":\(cost),"status":"exact",
        "tokens":\(tokens(total: events * 15)),"event_count":\(events)}
        """
    }

    private func tokens(total: Int) -> String {
        """
        {"input":\(total),"output":0,"cache_read":0,"cache_write":0,"reasoning":0,"total":\(total)}
        """
    }
}
