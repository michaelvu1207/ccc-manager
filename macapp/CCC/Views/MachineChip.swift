import SwiftUI

/// One machine on an account row. Its look says what the account is there:
/// filled = active, outline = stored (click switches), orange = needs login
/// (click re-logs in), dashed "+" = not there (click starts a login there),
/// dimmed = machine offline.
struct MachineChip: View {
    @EnvironmentObject private var state: AppState
    let account: ViewAccount
    let machine: ViewMachine

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
            label
        }
        .buttonStyle(.plain)
        .disabled(!clickable || state.pendingAction != nil)
        .onHover { hovering = $0 }
        .help(tooltip)
        .accessibilityLabel("\(machine.name): \(stateText)")
        .contextMenu { menu }
    }

    // MARK: Look

    private var label: some View {
        HStack(spacing: 3) {
            if pending {
                ProgressView().controlSize(.mini).scaleEffect(0.7).frame(width: 10, height: 10)
            } else {
                Image(systemName: glyph)
                    .font(.system(size: 8, weight: .bold))
            }
            Text(machine.shortName)
                .lineLimit(1)
            if presence.isDisabled {
                Image(systemName: "pause.circle")
                    .font(.system(size: 8))
                    .help("Disabled on \(machine.name)")
            }
        }
        .font(.caption2.weight(presence.state == .active ? .semibold : .regular))
        .foregroundStyle(foreground)
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(background, in: Capsule())
        .overlay(border)
        .opacity(presence.state == .offline ? 0.45 : 1)
        .contentShape(Capsule())
        .scaleEffect(hovering && clickable ? 1.04 : 1)
        .animation(.easeOut(duration: 0.1), value: hovering)
    }

    private var glyph: String {
        switch presence.state {
        case .active: return "checkmark"
        case .stored: return platformSymbol(machine.platform, local: machine.local)
        case .needsLogin: return "exclamationmark"
        case .absent: return "plus"
        case .offline: return "wifi.slash"
        }
    }

    private var foreground: Color {
        switch presence.state {
        case .active: return .white
        case .stored: return .primary
        case .needsLogin: return .orange
        case .absent, .offline: return .secondary
        }
    }

    private var background: Color {
        switch presence.state {
        case .active: return .accentColor
        case .needsLogin: return Color.orange.opacity(0.14)
        case .stored: return hovering ? Color.primary.opacity(0.08) : .clear
        case .absent: return hovering ? Color.primary.opacity(0.06) : .clear
        case .offline: return Color.primary.opacity(0.04)
        }
    }

    @ViewBuilder
    private var border: some View {
        switch presence.state {
        case .active:
            EmptyView()
        case .stored:
            Capsule().strokeBorder(Color.secondary.opacity(0.55), lineWidth: 1)
        case .needsLogin:
            Capsule().strokeBorder(Color.orange.opacity(0.7), lineWidth: 1)
        case .absent:
            Capsule().strokeBorder(Color.secondary.opacity(0.5),
                                   style: StrokeStyle(lineWidth: 1, dash: [3, 2]))
        case .offline:
            Capsule().strokeBorder(Color.secondary.opacity(0.3), lineWidth: 1)
        }
    }

    // MARK: Behavior

    private var clickable: Bool {
        switch presence.state {
        case .stored, .needsLogin: return true
        case .absent: return isClaude || machine.local
        case .active, .offline: return false
        }
    }

    private var stateText: String {
        switch presence.state {
        case .active: return "active"
        case .stored: return "stored — click to switch this machine to it"
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
        return text
    }

    @ViewBuilder
    private var menu: some View {
        Text(machine.name)
        if presence.state == .stored {
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
