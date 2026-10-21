import XCTest
@testable import CCC

final class AutoSwitchTests: XCTestCase {
    private static let machinesJSON = """
    [{"id":"local","name":"This Mac","short_name":"Mac","platform":"Darwin","local":true,"online":true,"engine":"ccc"},
     {"id":"a100","name":"simforge-a100","short_name":"a100","platform":"Linux","local":false,"online":true,"engine":"cswap"},
     {"id":"laptop","name":"simforge-laptop","short_name":"laptop","platform":"Darwin","local":false,"online":false,"engine":"ccc"}]
    """

    /// `used5h` / `usedWk` are percent used; `states` maps machine → presence state.
    private func account(_ email: String, provider: String = "claude", used5h: Double, usedWk: Double = 10,
                         states: [String: String], disabledOn: Set<String> = [],
                         needsLogin: Bool = false) -> String {
        let machines = states.map { id, state in
            state == "absent" ? #""\#(id)":{"state":"absent"}"#
                : #""\#(id)":{"state":"\#(state)","num":"\#(email.prefix(1))","query":"q-\#(email)","disabled":\#(disabledOn.contains(id))}"#
        }.joined(separator: ",")
        return """
        {"key":"\(provider)|\(email)|o","provider":"\(provider)","email":"\(email)","alias":"","label":"\(email)",
         "org_name":"","plan":"max","windows":[{"id":"5h","pct_used":\(used5h),"resets_in":"1h"},{"id":"wk","pct_used":\(usedWk),"resets_in":"3d"}],
         "usage_stale":false,"usage_from":"local","usage_at":1,"status_message":"","needs_login":\(needsLogin),
         "disabled":false,"disabled_on":[],"active_on":[],"stored_on":[],"machines":{\(machines)},
         "consumable_resets":null,"consumable_reset_credits":[]}
        """
    }

    private func view(_ accounts: [String]) throws -> AccountsView {
        let json = #"{"machines":\#(Self.machinesJSON),"accounts":[\#(accounts.joined(separator: ","))]}"#
        return try JSONDecoder().decode(AccountsView.self, from: Data(json.utf8))
    }

    func testNumbersRunTopToBottomPerProviderAndSkipExcluded() throws {
        let view = try view([
            account("a@x", used5h: 0, states: ["local": "active"]),
            account("b@x", used5h: 0, states: ["local": "stored"]),
            account("c@x", used5h: 0, states: ["local": "stored"]),
            account("d@x", provider: "codex", used5h: 0, states: ["local": "active"]),
        ])
        let order = AutoSwitch.order(view.accounts, excluded: ["claude|b@x|o"])
        XCTAssertEqual(order, ["claude|a@x|o": 1, "claude|c@x|o": 2, "codex|d@x|o": 1])
    }

    func testLowAccountMovesToFirstNumberedViableAccount() throws {
        let view = try view([
            account("a@x", used5h: 96, states: ["local": "active", "a100": "active"]),
            account("b@x", used5h: 97, states: ["local": "stored", "a100": "stored"]),  // low too
            account("c@x", used5h: 0, states: ["local": "stored", "a100": "stored"]),   // excluded
            account("d@x", used5h: 50, states: ["local": "stored", "a100": "stored"], disabledOn: ["a100"]),
            account("e@x", used5h: 0, states: ["local": "stored", "a100": "stored"]),
        ])
        let moves = AutoSwitch.plan(view, excluded: ["claude|c@x|o"])
        XCTAssertEqual(moves.map(\.machine), ["local", "a100"])
        XCTAssertEqual(moves.map(\.to), ["d@x", "e@x"])  // d is disabled on a100
        XCTAssertEqual(moves.first?.query, "q-d@x")
        XCTAssertEqual(moves.first?.fromLeft, 4)
    }

    func testWeeklyWindowAloneTriggersAtExactlyFivePercentLeft() throws {
        let view = try view([
            account("a@x", used5h: 0, usedWk: 95, states: ["local": "active"]),
            account("b@x", used5h: 0, states: ["local": "stored"]),
        ])
        XCTAssertEqual(AutoSwitch.plan(view, excluded: []).map(\.to), ["b@x"])
    }

    func testNoMoveAboveFivePercentOfflineOrWithoutTarget() throws {
        let healthy = try view([
            account("a@x", used5h: 94, states: ["local": "active"]),
            account("b@x", used5h: 0, states: ["local": "stored"]),
        ])
        XCTAssertTrue(AutoSwitch.plan(healthy, excluded: []).isEmpty)

        let offline = try view([
            account("a@x", used5h: 99, states: ["laptop": "active"]),
            account("b@x", used5h: 0, states: ["laptop": "stored"]),
        ])
        XCTAssertTrue(AutoSwitch.plan(offline, excluded: []).isEmpty)

        let noTarget = try view([
            account("a@x", used5h: 99, states: ["local": "active"]),
            account("b@x", used5h: 0, states: ["local": "pending"]),
            account("c@x", used5h: 0, states: ["local": "stored"], needsLogin: true),
            account("d@x", used5h: 0, states: ["a100": "stored"]),
        ])
        XCTAssertTrue(AutoSwitch.plan(noTarget, excluded: []).isEmpty)
    }

    func testCodexSwitchesWithinCodexOnly() throws {
        let view = try view([
            account("a@x", used5h: 0, states: ["local": "stored"]),
            account("b@x", provider: "codex", used5h: 100, states: ["local": "active"]),
            account("c@x", provider: "codex", used5h: 20, states: ["local": "stored"]),
        ])
        let moves = AutoSwitch.plan(view, excluded: [])
        XCTAssertEqual(moves.map(\.provider), ["codex"])
        XCTAssertEqual(moves.map(\.to), ["c@x"])
    }
}
