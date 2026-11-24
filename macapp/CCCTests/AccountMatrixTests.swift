import AppKit
import SwiftUI
import XCTest
@testable import CCC

@MainActor
final class AccountMatrixTests: XCTestCase {
    private static let machinesJSON = """
    [{"id":"local","name":"Michael's MacBook Pro","short_name":"Mac","platform":"Darwin","local":true,"online":true,"engine":"ccc"},
     {"id":"brower","name":"work-desktop","short_name":"work","platform":"Linux","local":false,"online":true,"engine":"cswap"},
     {"id":"a100","name":"studio-a100","short_name":"a100","platform":"Linux","local":false,"online":true,"engine":"cswap"},
     {"id":"laptop","name":"studio-laptop","short_name":"laptop","platform":"Darwin","local":false,"online":false,"engine":"ccc"}]
    """

    private func account(email: String, provider: String = "claude", shared: Bool? = true,
                         states: [String: String], active: [String] = []) throws -> ViewAccount {
        let machines = states.map { id, state in
            state == "absent" ? #""\#(id)":{"state":"absent"}"#
                : #""\#(id)":{"state":"\#(state)","num":"1","query":"1","disabled":false}"#
        }.joined(separator: ",")
        let stored = states.filter { $0.value != "absent" }.keys.map { "\"\($0)\"" }.joined(separator: ",")
        let json = """
        {"key":"\(provider)|\(email)|o","provider":"\(provider)","email":"\(email)","alias":"","label":"\(email)",
         "org_name":"","plan":"max","windows":[{"id":"5h","pct_used":20,"resets_in":"1h"},{"id":"wk","pct_used":50,"resets_in":"3d"}],
         "usage_stale":false,"usage_from":"local","usage_at":1,"status_message":"","needs_login":false,
         "disabled":false,"disabled_on":[],"active_on":[\(active.map { "\"\($0)\"" }.joined(separator: ","))],
         "stored_on":[\(stored)],\(shared.map { "\"shared\":\($0)," } ?? "")"machines":{\(machines)},
         "consumable_resets":null,"consumable_reset_credits":[]}
        """
        return try JSONDecoder().decode(ViewAccount.self, from: Data(json.utf8))
    }

    private func machines() throws -> [ViewMachine] {
        try JSONDecoder().decode([ViewMachine].self, from: Data(Self.machinesJSON.utf8))
    }

    // MARK: Layout

    func testFourMachinesFitThePopoverWithNamedColumns() {
        let layout = MatrixLayout(machines: 4)
        XCTAssertEqual(layout.columnWidth, 46)
        XCTAssertFalse(layout.iconOnly)
        XCTAssertGreaterThanOrEqual(layout.labelWidth, MatrixLayout.minLabelWidth)
        let right = layout.columnX(3) + layout.columnWidth + MatrixLayout.gap + MatrixLayout.menuWidth
            + MatrixLayout.rowInset
        XCTAssertEqual(right, MatrixLayout.popoverContentWidth)
        print("MATRIX 4 machines: label \(layout.labelWidth) pt, columns at x = "
              + (0..<4).map { "\(layout.columnX($0))" }.joined(separator: ", ")
              + ", \(layout.columnWidth) pt each, menu at \(layout.columnX(4) + MatrixLayout.gap)")
    }

    func testManyMachinesShrinkToIconColumnsInsteadOfScrolling() {
        let six = MatrixLayout(machines: 6)
        XCTAssertTrue(six.iconOnly)
        XCTAssertGreaterThanOrEqual(six.labelWidth, MatrixLayout.minLabelWidth)
        XCTAssertLessThanOrEqual(six.columnX(6) + MatrixLayout.gap + MatrixLayout.menuWidth + MatrixLayout.rowInset,
                                 MatrixLayout.popoverContentWidth)
        print("MATRIX 6 machines: label \(six.labelWidth) pt, \(six.columnWidth) pt columns, icon-only")
    }

    // MARK: Rendering at popover width

    func testRenderedMatrixFitsPopoverWidth() throws {
        let machines = try machines()
        let layout = MatrixLayout(machines: machines.count)
        let long = try account(email: "a-very-long-address.for-testing@long-example.com",
                               states: ["local": "stored", "brower": "active", "a100": "active",
                                        "laptop": "offline"], active: ["brower", "a100"])
        let codex = try account(email: "codex@example.com", provider: "codex", shared: nil,
                                states: ["local": "active", "brower": "absent", "a100": "needs_login",
                                         "laptop": "pending"], active: ["local"])
        let state = AppState()
        let content = VStack(alignment: .leading, spacing: 0) {
            MatrixHeader(machines: machines, layout: layout)
                .hidden()
                .overlay(alignment: .leading) {  // Menus do not render offscreen: draw their faces
                    HStack(spacing: 0) {
                        Color.clear.frame(width: layout.labelWidth + MatrixLayout.gap, height: 1)
                        ForEach(machines) { MatrixHeaderLabel(machine: $0, layout: layout) }
                    }
                    .padding(.horizontal, MatrixLayout.rowInset)
                }
            AccountCard(account: long, machines: machines, layout: layout)
            AccountCard(account: codex, machines: machines, layout: layout)
        }
        .environmentObject(state)
        .frame(width: MatrixLayout.popoverContentWidth, alignment: .leading)

        let host = NSHostingView(rootView: content)
        host.frame = NSRect(x: 0, y: 0, width: MatrixLayout.popoverContentWidth, height: 400)
        host.layoutSubtreeIfNeeded()
        let fitting = host.fittingSize
        XCTAssertLessThanOrEqual(fitting.width, MatrixLayout.popoverContentWidth)

        // Each part alone must not ask for more than the popover width.
        for (name, view) in [
            ("header", AnyView(MatrixHeader(machines: machines, layout: layout).environmentObject(state))),
            ("row", AnyView(AccountCard(account: long, machines: machines, layout: layout).environmentObject(state))),
        ] {
            let part = NSHostingView(rootView: view)
            XCTAssertLessThanOrEqual(part.fittingSize.width, MatrixLayout.popoverContentWidth + 0.5, name)
            print("MATRIX \(name) fitting width: \(part.fittingSize.width) pt, height \(part.fittingSize.height) pt")
        }

        // A picture for visual review (not compared).
        let renderer = ImageRenderer(content: content.padding(10).background(Color(nsColor: .windowBackgroundColor)))
        renderer.scale = 2
        if let image = renderer.nsImage, let tiff = image.tiffRepresentation,
           let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("ccc-matrix.png")
            try png.write(to: url)
            print("MATRIX render: \(url.path) \(image.size)")
        }
    }

    // MARK: The row does nothing on click; cells switch

    func testAccountRowHasNoClickActionOrLeadingDot() throws {
        let source = try String(contentsOf: URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("CCC/Views/AccountCard.swift"), encoding: .utf8)
        XCTAssertFalse(source.contains("onTapGesture"))
        XCTAssertFalse(source.contains("switchHere("), "only a machine cell switches")
        XCTAssertFalse(source.contains("\"circle.fill\""), "no leading active dot")
        XCTAssertFalse(source.contains("arrow.triangle.2.circlepath"), "no sync icon")
    }

    // MARK: Re-login and delete

    func testReloginPlan() throws {
        let machines = try machines()
        let shared = try account(email: "a@x.com", states: ["local": "stored", "a100": "needs_login"])
        XCTAssertEqual(AppState.reloginPlan(account: shared, machines: machines, syncEnabled: true),
                       .syncLogin(email: "a@x.com"))
        XCTAssertEqual(LoginLauncher.syncLoginArguments(email: "a@x.com"), ["sync", "login", "--email", "a@x.com"])
        // Without sync: the machine that needs the login, with its slot.
        XCTAssertEqual(AppState.reloginPlan(account: shared, machines: machines, syncEnabled: false),
                       .machineLogin(machine: "a100", num: "1"))
        let local = try account(email: "b@x.com", shared: nil, states: ["brower": "stored", "local": "stored"])
        XCTAssertEqual(AppState.reloginPlan(account: local, machines: machines, syncEnabled: false),
                       .machineLogin(machine: "local", num: "1"))
        let unshared = try account(email: "c@x.com", shared: false, states: ["brower": "stored"])
        XCTAssertEqual(AppState.reloginPlan(account: unshared, machines: machines, syncEnabled: true),
                       .machineLogin(machine: "brower", num: "1"))
        let codex = try account(email: "d@x.com", provider: "codex", shared: nil, states: ["brower": "stored"])
        XCTAssertEqual(AppState.reloginPlan(account: codex, machines: machines, syncEnabled: true),
                       .codex(machine: "local"))
    }

    func testDeleteWarningNamesWhereActive() throws {
        let machines = try machines()
        let names = Dictionary(uniqueKeysWithValues: machines.map { ($0.id, $0.name) })
        let acct = try account(email: "a@x.com", states: ["brower": "active", "a100": "active", "local": "stored"],
                               active: ["brower", "a100"])
        XCTAssertEqual(AppState.deleteWarning(account: acct, name: { names[$0] ?? $0 }, syncing: true),
                       "Claude Code on work-desktop and studio-a100 is using this account; "
                       + "it keeps working until it switches, but CCC stops syncing it.")
        XCTAssertEqual(AppState.deleteWarning(account: acct, name: { names[$0] ?? $0 }, syncing: false),
                       "Claude Code on work-desktop and studio-a100 is using this account; "
                       + "it keeps working until it switches.")
        let idle = try account(email: "b@x.com", states: ["local": "stored"])
        XCTAssertNil(AppState.deleteWarning(account: idle, name: { $0 }, syncing: true))
        XCTAssertEqual(CCCClient.deleteArguments(key: "claude|a@x.com|o", provider: "claude"),
                       ["delete", "--provider", "claude", "--yes", "--", "claude|a@x.com|o"])
    }

    func testShortNameAndHubDeletedDecode() throws {
        let config = try JSONDecoder().decode(MachineConfig.self, from: Data(
            #"{"id":"a100","name":"studio-a100","alias":"a100","local":false,"enabled":true,"engine":"auto","short":"gpu"}"#.utf8))
        XCTAssertEqual(config.short, "gpu")
        let old = try JSONDecoder().decode(MachineConfig.self, from: Data(
            #"{"id":"a","name":"a","alias":"a","local":false,"enabled":true,"engine":"auto"}"#.utf8))
        XCTAssertNil(old.short)
        XCTAssertNil(try account(email: "a@x.com", states: [:]).hubDeleted)
    }
}
