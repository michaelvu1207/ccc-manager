import SwiftUI

/// Column geometry of the Accounts tab: account label on the left, one
/// fixed-width column per machine, then the "…" menu. Header and rows use
/// the same layout, so every cell sits exactly under its machine.
/// Columns shrink (icon-only headers) before the popover would scroll.
struct MatrixLayout: Equatable {
    /// The popover is 360 pt wide with 10 pt padding.
    static let popoverContentWidth: CGFloat = 340
    static let rowInset: CGFloat = 8
    static let menuWidth: CGFloat = 18
    static let gap: CGFloat = 6
    static let minLabelWidth: CGFloat = 100
    static let fullColumn: CGFloat = 46
    /// Below this, headers show the platform icon only (name in the tooltip).
    static let namedColumn: CGFloat = 40
    static let minColumn: CGFloat = 18

    let width: CGFloat
    let machines: Int
    let columnWidth: CGFloat
    let labelWidth: CGFloat

    init(width: CGFloat = MatrixLayout.popoverContentWidth, machines: Int) {
        self.width = width
        self.machines = machines
        let fixed = 2 * Self.rowInset + Self.menuWidth + 2 * Self.gap
        let room = width - fixed - Self.minLabelWidth
        let count = CGFloat(max(1, machines))
        columnWidth = max(Self.minColumn, min(Self.fullColumn, (room / count).rounded(.down)))
        labelWidth = width - fixed - count * columnWidth
    }

    var iconOnly: Bool { columnWidth < Self.namedColumn }

    /// Leading x of machine column `index`, from the row's left edge.
    func columnX(_ index: Int) -> CGFloat {
        Self.rowInset + labelWidth + Self.gap + CGFloat(index) * columnWidth
    }
}

/// Column headers: platform icon (online dot) + short name per machine.
/// Clicking a header opens that machine's menu.
struct MatrixHeader: View {
    @EnvironmentObject private var state: AppState
    let machines: [ViewMachine]
    let layout: MatrixLayout

    var body: some View {
        HStack(spacing: 0) {
            Color.clear.frame(width: layout.labelWidth + MatrixLayout.gap, height: 1)
            ForEach(machines) { machine in
                header(machine).frame(width: layout.columnWidth)
            }
            Color.clear.frame(width: MatrixLayout.gap + MatrixLayout.menuWidth, height: 1)
        }
        .padding(.horizontal, MatrixLayout.rowInset)
    }

    // A Menu would flatten the custom label into one "icon + title" line and
    // truncate the name, so the header is a plain view: click refreshes that
    // machine, right-click shows the machine menu.
    private func header(_ machine: ViewMachine) -> some View {
        MatrixHeaderLabel(machine: machine, layout: layout,
                          loading: state.loadingMachines.contains(machine.id))
            .contentShape(Rectangle())
            .onTapGesture { Task { await state.refresh(machineID: machine.id) } }
            .contextMenu {
                Text(machine.name + (machine.online ? "" : " — offline"))
                Button("Add a Claude account here…") { state.addAccount(machineID: machine.id) }
                    .disabled(!machine.online)
                Button("Refresh") { Task { await state.refresh(machineID: machine.id) } }
            }
            .help("\(machine.name) — \(machine.online ? "online" : "offline") · click to refresh, right-click for more")
        .accessibilityLabel("\(machine.name), \(machine.online ? "online" : "offline")")
    }
}

/// A column header's face: platform icon with an online dot, short name below.
struct MatrixHeaderLabel: View {
    let machine: ViewMachine
    let layout: MatrixLayout
    var loading = false

    var body: some View {
        VStack(spacing: 1) {
            ZStack(alignment: .bottomTrailing) {
                if loading {
                    ProgressView().controlSize(.mini).scaleEffect(0.6).frame(width: 12, height: 11)
                } else {
                    Image(systemName: platformSymbol(machine.platform, local: machine.local))
                        .font(.system(size: 10))
                        .frame(width: 14, height: 11)
                    Circle()
                        .fill(machine.online ? Color.green : Color.red)
                        .frame(width: 4, height: 4)
                        .offset(x: 2, y: 1)
                }
            }
            if !layout.iconOnly {
                Text(machine.shortName)
                    .font(.system(size: 9))
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
        }
        .foregroundStyle(.secondary)
        .frame(width: layout.columnWidth)
        .contentShape(Rectangle())
    }
}

/// One account on one machine: a marker, no text. Click = switch to it /
/// re-login / sign in there; right-click = more. A subtle orange outline
/// means that machine's copy of a shared login is behind the hub.
struct MachineCell: View {
    @EnvironmentObject private var state: AppState
    let account: ViewAccount
    let machine: ViewMachine
    let width: CGFloat

    @State private var hovering = false

    private var presence: MachinePresence {
        account.presence(on: machine.id)
            ?? MachinePresence(state: .offline, num: nil, query: nil, disabled: nil, lastState: nil)
    }
    private var pending: Bool { state.pendingAction == state.chipKey(account, machine.id) }
    private var isClaude: Bool { account.provider == "claude" }

    var body: some View {
        Button {
            Task { await state.tapChip(account, machine: machine) }
        } label: {
            ZStack {
                if hovering && clickable {
                    RoundedRectangle(cornerRadius: 4).fill(Color.primary.opacity(0.08))
                        .frame(width: 22, height: 16)
                }
                if syncWarning {
                    RoundedRectangle(cornerRadius: 4)
                        .strokeBorder(Color.orange.opacity(0.6), lineWidth: 1)
                        .frame(width: 22, height: 16)
                }
                if pending {
                    ProgressView().controlSize(.mini).scaleEffect(0.6)
                } else {
                    Image(systemName: Self.symbol(presence.state))
                        .font(.system(size: presence.state == .absent ? 8 : 10,
                                      weight: presence.state == .absent ? .regular : .semibold))
                        .foregroundStyle(color)
                }
            }
            .frame(width: width, height: 18)
            .opacity(presence.state == .offline ? 0.45 : (presence.isDisabled ? 0.5 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!clickable || state.pendingAction != nil)
        .onHover { hovering = $0 }
        .help(tooltip)
        .accessibilityLabel("\(machine.name): \(stateText)")
        .contextMenu { menu }
    }

    static func symbol(_ state: MachinePresence.State) -> String {
        switch state {
        case .active: return "circle.fill"
        case .stored: return "circle"
        case .pending: return "circle.dotted"
        case .needsLogin: return "exclamationmark.circle.fill"
        case .absent: return "plus"
        case .offline: return "xmark"
        }
    }

    private var color: Color {
        switch presence.state {
        case .active: return .accentColor
        case .stored: return Color.primary.opacity(0.7)
        case .pending: return .secondary
        case .needsLogin: return .orange
        case .absent: return Color.secondary.opacity(0.5)
        case .offline: return .secondary
        }
    }

    /// Shared login whose copy here is behind the hub, or no agent reports.
    private var syncWarning: Bool {
        guard let sync = presence.sync, presence.state != .offline else { return false }
        return sync.state != "in_sync"
    }

    private var clickable: Bool {
        switch presence.state {
        case .stored, .pending, .needsLogin: return true
        case .absent: return isClaude || machine.local
        case .active, .offline: return false
        }
    }

    private var stateText: String {
        switch presence.state {
        case .active: return "active"
        case .stored: return "stored — click to switch this machine to it"
        case .pending: return "\(presence.note ?? "usage pending") — shared login, click to switch this machine to it"
        case .needsLogin: return "needs login — click to sign in there"
        case .absent: return clickable ? "not on this machine — click to sign in there"
                                       : "not on this machine — run codex login there"
        case .offline:
            let last = presence.lastState.map { $0 == "unknown" ? "" : " (last seen \($0.replacingOccurrences(of: "_", with: " ")))" } ?? ""
            return "offline\(last)"
        }
    }

    private var tooltip: String {
        var text = "\(machine.name): \(stateText)"
        if presence.isDisabled { text += " · disabled here" }
        if let sync = presence.sync {
            switch sync.state {
            case "in_sync":
                text += " · shared login in sync (gen \(sync.hubGeneration ?? 0))"
            case "behind":
                text += " · shared login behind the hub (gen \(sync.generation ?? 0) of \(sync.hubGeneration ?? 0))"
            default:
                text += " · shared, but no sync agent reports from this machine"
            }
            if let last = sync.lastSync {
                text += ", synced \(MachineSection.age(since: last)) ago"
            }
        }
        return text
    }

    @ViewBuilder
    private var menu: some View {
        Text(machine.name)
        if presence.state == .stored || presence.state == .pending {
            Button("Switch here") { Task { await state.switchHere(account, machine: machine.id) } }
        }
        if presence.isPresent && presence.state != .offline && (isClaude || machine.local) {
            Button("Re-login…") { state.reLogin(account, machine: machine.id) }
        }
        if presence.state == .absent && clickable {
            Button("Sign in here…") { Task { await state.tapChip(account, machine: machine) } }
        }
        if isClaude && presence.isPresent && presence.state != .offline {
            Button(presence.isDisabled ? "Enable on this machine" : "Disable on this machine") {
                Task { await state.setDisabled(!presence.isDisabled, account: account, machine: machine.id) }
            }
        }
        if presence.isPresent && presence.state != .offline
            && (isClaude || machine.local || machine.engine == "ccc") {
            Divider()
            Button("Remove from this machine…", role: .destructive) {
                state.confirmingRemoval = state.chipKey(account, machine.id)
            }
        }
    }
}
