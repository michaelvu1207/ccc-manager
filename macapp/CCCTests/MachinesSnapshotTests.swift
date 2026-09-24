import XCTest
@testable import CCC

final class MachinesSnapshotTests: XCTestCase {
    private func decode(_ json: String) throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    private let account = """
    {"num":"2","email":"b@example.test","label":"b@example.test","plan":"",
     "active":true,"disabled":true,"alias":"work","status":"keychain_unavailable",
     "needs_login":false,"status_message":"keychain locked",
     "windows":[{"id":"fable","pct_used":75,"resets_in":"3d 1h"}],"usage_stale":false}
    """

    func testMachinesArrayDecodes() throws {
        let snapshot = try decode("""
        {"version":2,"generated_at":1,"providers":[],
         "machines":[
          {"id":"local","alias":null,"display_name":"Here","local":true,"engine":"ccc",
           "platform":"Darwin","online":true,"error":"","fetched_at":1,"stale":false,
           "has_data":true,"duplicate_of":null,"providers":[]},
          {"id":"box","alias":"box","display_name":"Box","local":false,"engine":"cswap",
           "platform":"Linux","online":false,"error":"offline: timed out","fetched_at":null,
           "stale":true,"has_data":true,"duplicate_of":null,
           "providers":[{"id":"claude","available":true,"error":"","accounts":[\(account)]}]},
          {"id":"twin","alias":"twin","display_name":"Twin","local":false,"engine":"cswap",
           "platform":"Darwin","online":true,"error":"","fetched_at":1,"stale":false,
           "has_data":true,"duplicate_of":"local","providers":[]}
         ]}
        """)
        let machines = snapshot.machineList
        XCTAssertEqual(machines.map(\.id), ["local", "box", "twin"])
        let box = machines[1]
        XCTAssertFalse(box.online)
        XCTAssertEqual(box.accountCount, 1)
        let acct = try XCTUnwrap(box.provider("claude")?.activeAccount)
        XCTAssertTrue(acct.isDisabled)
        XCTAssertEqual(acct.window("fable")?.pctLeft, 25)
        XCTAssertEqual(machines[2].duplicateOf, "local")
    }

    func testSnapshotWithoutMachinesFallsBackToThisMac() throws {
        let snapshot = try decode("""
        {"version":2,"generated_at":1,
         "providers":[{"id":"claude","available":true,"accounts":[]}]}
        """)
        XCTAssertNil(snapshot.machines)
        XCTAssertEqual(snapshot.machineList.map(\.id), ["local"])
        XCTAssertTrue(snapshot.machineList[0].local)
    }

    func testAccountWithoutNewFieldsIsNotDisabled() throws {
        let acct = try JSONDecoder().decode(Account.self, from: Data("""
        {"num":"1","email":"a@x","label":"a@x","plan":"","active":false,
         "needs_login":false,"windows":[],"usage_stale":false}
        """.utf8))
        XCTAssertFalse(acct.isDisabled)
        XCTAssertNil(acct.alias)
    }

    func testMachineListDecodes() throws {
        let list = try JSONDecoder().decode(MachineList.self, from: Data("""
        {"config_path":"/tmp/m.json","customized":false,
         "machines":[{"id":"local","name":"Here","alias":null,"local":true,"enabled":true,
                      "engine":"auto","resolved_engine":"ccc"}]}
        """.utf8))
        XCTAssertEqual(list.machines.first?.engine, "auto")
    }

    func testTestHostDoesNotAutoLoadAccounts() {
        XCTAssertTrue(AppState.isRunningTests)
    }

    func testAccountsViewDecodes() throws {
        let snapshot = try decode("""
        {"version":2,"generated_at":1,"providers":[],
         "accounts_view":{"generated_at":1,
          "machines":[
            {"id":"local","name":"Here","short_name":"this Mac","platform":"Darwin","local":true,"online":true,"engine":"ccc"},
            {"id":"box","name":"Box","short_name":"Box","platform":"Linux","local":false,"online":false,"engine":"cswap"}],
          "accounts":[{"key":"claude|a@x.com|o1","provider":"claude","email":"a@x.com","alias":"",
            "label":"a@x.com","org_name":"Org","plan":"","windows":[{"id":"5h","pct_used":20,"resets_in":"1h"}],
            "usage_stale":false,"usage_from":"box","usage_at":100,"status_message":"","needs_login":false,
            "disabled":false,"disabled_on":["box"],"active_on":["local"],"stored_on":["local","box"],
            "machines":{
              "local":{"state":"active","num":"1","query":"1","disabled":false},
              "box":{"state":"offline","num":"4","query":"4","disabled":true,"last_state":"stored"}},
            "consumable_resets":null,"consumable_reset_credits":[]}]}}
        """)
        let view = try XCTUnwrap(snapshot.accountsView)
        XCTAssertEqual(view.machines.map(\.shortName), ["this Mac", "Box"])
        let account = try XCTUnwrap(view.accounts.first)
        XCTAssertEqual(account.presence(on: "local")?.state, .active)
        let box = try XCTUnwrap(account.presence(on: "box"))
        XCTAssertEqual(box.state, .offline)
        XCTAssertEqual(box.num, "4")
        XCTAssertEqual(box.lastState, "stored")
        XCTAssertTrue(box.isDisabled)
        XCTAssertTrue(account.mixedDisabled)
        XCTAssertEqual(account.windows.first?.pctLeft, 80)
    }

    func testAbsentPresenceHasNoSlot() throws {
        let presence = try JSONDecoder().decode(MachinePresence.self, from: Data(#"{"state":"absent"}"#.utf8))
        XCTAssertEqual(presence.state, .absent)
        XCTAssertFalse(presence.isPresent)
        let needs = try JSONDecoder().decode(MachinePresence.self, from: Data(#"{"state":"needs_login","num":"2"}"#.utf8))
        XCTAssertEqual(needs.state, .needsLogin)
    }
}
