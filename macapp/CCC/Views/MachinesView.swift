import SwiftUI

/// Settings screen inside the popover: the machine list from
/// ~/.config/ccc/machines.json (via `ccc machines`).
struct MachinesView: View {
    @EnvironmentObject private var state: AppState

    @State private var newAlias = ""
    @State private var newName = ""
    @State private var adding = false
    @FocusState private var aliasFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) { state.showingMachines = false }
                } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 11, weight: .semibold))
                        .frame(width: 18, height: 18)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Back to accounts")
                Text("Machines")
                    .font(.headline)
                Spacer()
                Button {
                    Task { await state.importFromStarline() }
                } label: {
                    Label("Import from Starline", systemImage: "square.and.arrow.down")
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(Color.accentColor)
                .help("Add SSH hosts from Starline's connection list")
            }
            .padding(.horizontal, 4)

            if let list = state.machineList {
                ScrollView {
                    VStack(spacing: 2) {
                        ForEach(list.machines) { machine in
                            MachineConfigRow(machine: machine)
                        }
                    }
                }
                .frame(maxHeight: 420)
                .fixedSize(horizontal: false, vertical: true)
                addForm
                SyncSection()
                AutoSwitchSection()
                Text(list.customized
                     ? "Saved in \(abbreviated(list.configPath))"
                     : "Default list: this Mac + Starline hosts. Changes are saved to \(abbreviated(list.configPath)).")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 4)
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 20)
            }
        }
        .task { await state.loadMachineList() }
    }

    private var addForm: some View {
        HStack(spacing: 6) {
            TextField("SSH alias", text: $newAlias)
                .focused($aliasFocused)
                .frame(width: 120)
            TextField("Name (optional)", text: $newName)
            Button {
                submit()
            } label: {
                if adding {
                    ProgressView().controlSize(.mini)
                } else {
                    Text("Add")
                }
            }
            .disabled(newAlias.trimmingCharacters(in: .whitespaces).isEmpty || adding)
        }
        .textFieldStyle(.roundedBorder)
        .controlSize(.small)
        .onSubmit(submit)
        .padding(.horizontal, 4)
        .padding(.top, 4)
    }

    private func submit() {
        let alias = newAlias.trimmingCharacters(in: .whitespaces)
        guard !alias.isEmpty, !adding else { return }
        adding = true
        Task {
            if await state.addMachine(alias: alias, name: newName) {
                newAlias = ""
                newName = ""
            }
            adding = false
        }
    }

    private func abbreviated(_ path: String) -> String {
        (path as NSString).abbreviatingWithTildeInPath
    }
}

private struct MachineConfigRow: View {
    @EnvironmentObject private var state: AppState
    let machine: MachineConfig

    @State private var editing = false
    @State private var draft = ""
    @State private var shortDraft = ""
    @State private var hovering = false
    @FocusState private var nameFocused: Bool

    private var snapshot: MachineSnapshot? { state.machines.first { $0.id == machine.id } }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: platformSymbol(snapshot?.platform ?? "", local: machine.local))
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .frame(width: 18)

            VStack(alignment: .leading, spacing: 1) {
                if editing {
                    HStack(spacing: 4) {
                        TextField("Name", text: $draft)
                            .textFieldStyle(.roundedBorder)
                            .controlSize(.small)
                            .focused($nameFocused)
                            .onSubmit(commit)
                            .onExitCommand { editing = false }
                        TextField("Short", text: $shortDraft)
                            .textFieldStyle(.roundedBorder)
                            .controlSize(.small)
                            .frame(width: 64)
                            .onSubmit(commit)
                            .onExitCommand { editing = false }
                            .help("Column label on the Accounts tab (up to 10 characters; empty = automatic)")
                    }
                } else {
                    HStack(spacing: 5) {
                        Text(machine.name)
                            .font(.callout)
                            .foregroundStyle(machine.enabled ? .primary : .secondary)
                            .lineLimit(1)
                        if let snapshot, machine.enabled {
                            if state.isLoading(snapshot) {
                                ProgressView().controlSize(.mini)
                            } else {
                                StatusDot(machine: snapshot)
                            }
                        }
                    }
                }
                Text(subtitle)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }

            Spacer(minLength: 4)

            HStack(spacing: 8) {
                Button {
                    if editing { commit() } else { startEditing() }
                } label: {
                    Image(systemName: editing ? "checkmark" : "pencil")
                }
                .help(editing ? "Save" : "Rename / short name")

                if !machine.local {
                    Button {
                        Task { await state.removeMachine(machine) }
                    } label: {
                        Image(systemName: "trash")
                    }
                    .help("Remove from the list (accounts on the machine are untouched)")
                }
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .opacity(hovering || editing ? 1 : 0)

            Toggle("", isOn: Binding(
                get: { machine.enabled },
                set: { enabled in Task { await state.setPolling(enabled, machine: machine) } }
            ))
            .toggleStyle(.switch)
            .controlSize(.mini)
            .labelsHidden()
            .help(machine.enabled ? "Polling — click to pause" : "Paused — click to poll")
        }
        .padding(.vertical, 5)
        .padding(.horizontal, 6)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(hovering ? Color.primary.opacity(0.05) : .clear)
        )
        .onHover { hovering = $0 }
    }

    private var subtitle: String {
        var parts = [machine.local ? "this Mac" : "ssh \(machine.alias ?? "")"]
        if let column = state.view?.machines.first(where: { $0.id == machine.id })?.shortName {
            parts.append("“\(column)”")
        }
        if !machine.enabled {
            parts.append("paused")
        } else if let snapshot {
            if let twin = snapshot.duplicateOf {
                parts.append("same machine as \(state.twinName(twin))")
            } else if snapshot.hasData && !snapshot.online {
                parts.append("offline")
            } else if snapshot.hasData {
                let count = snapshot.accountCount
                parts.append(count == 1 ? "1 account" : "\(count) accounts")
            }
        }
        return parts.joined(separator: " · ")
    }

    private func startEditing() {
        draft = machine.name
        shortDraft = machine.short ?? ""
        editing = true
        nameFocused = true
    }

    private func commit() {
        editing = false
        let (name, short) = (draft, String(shortDraft.prefix(10)))
        Task {
            await state.renameMachine(machine, to: name)
            await state.setShortName(machine, to: short)
        }
    }
}

/// Shared-login sync: hub, reachability, and each machine's last report.
private struct AutoSwitchSection: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Divider().padding(.vertical, 4)
            HStack(spacing: 6) {
                Image(systemName: "arrow.left.arrow.right")
                    .foregroundStyle(.secondary)
                Text("Auto-switch")
                    .font(.callout.weight(.semibold))
                Spacer()
                if state.autoSwitchBusy { ProgressView().controlSize(.mini) }
                Toggle("", isOn: Binding(
                    get: { state.autoSwitchOn },
                    set: { on in Task { await state.setAutoSwitch(on) } }
                ))
                .toggleStyle(.switch)
                .controlSize(.mini)
                .labelsHidden()
                .disabled(state.autoSwitch == nil || state.autoSwitchBusy)
                .accessibilityLabel("Auto-switch Claude accounts on every machine")
            }
            let left = state.autoSwitchPercentLeft
            Text("Each machine switches its Claude login when the active account has \(left.usage)% or less left "
                 + "(5h or weekly) or \(left.fable)% or less Fable left. It picks the account with the most left.")
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            if let list = state.autoSwitch {
                ForEach(list) { machine in
                    HStack(spacing: 6) {
                        Text(machine.name).font(.caption)
                        Spacer()
                        Text(detail(machine))
                            .font(.caption2)
                            .foregroundStyle(machine.ok ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.orange))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        if machine.ok, machine.installed == true {
                            Picker("", selection: Binding(
                                get: { machine.mode },
                                set: { mode in Task { await state.setLeftover(mode, on: machine.id) } }
                            )) {
                                Text("Normal").tag("off")
                                Text("Leftover Fable").tag("fable")
                                Text("Leftover weekly").tag("weekly")
                            }
                            .pickerStyle(.menu)
                            .labelsHidden()
                            .controlSize(.mini)
                            .fixedSize()
                            .disabled(state.autoSwitchBusy)
                            .accessibilityLabel("Auto-switch mode on \(machine.name)")
                        }
                    }
                    .help(lastSwitchHelp(machine))
                }
            } else {
                Text("Reading each machine…")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            leftover
            autoReset
        }
        .padding(.horizontal, 4)
        .task { await state.loadAutoSwitch() }
    }

    @ViewBuilder private var leftover: some View {
        Text("Leftover Fable: uses only accounts the normal machines left at their limits, until their Fable "
             + "or weekly quota is used up. Leftover weekly: ignores Fable and uses the weekly quota of accounts "
             + "with no Fable left, until it is used up (run Opus or Sonnet there); accounts with a limit reset "
             + "left go first. Both take the account whose week ends first, and a fresh account only when there "
             + "are no leftovers.")
            .font(.caption2)
            .foregroundStyle(.tertiary)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.top, 2)
    }

    @ViewBuilder private var autoReset: some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.counterclockwise.circle")
                .foregroundStyle(.secondary)
            Text("Auto-reset")
                .font(.caption.weight(.semibold))
            Spacer()
            Toggle("", isOn: Binding(
                get: { state.autoResetMachine != nil },
                set: { on in Task { await state.setAutoReset(on) } }
            ))
            .toggleStyle(.switch)
            .controlSize(.mini)
            .labelsHidden()
            .disabled(state.autoSwitch == nil || state.autoSwitchBusy)
            .accessibilityLabel("Use Claude limit resets automatically")
        }
        .padding(.top, 4)
        let runsOn = state.autoResetMachine?.name
            ?? state.autoSwitch?.first { $0.id == state.autoResetTarget }?.name ?? "one machine"
        Text("Uses a Claude limit reset only when every account is used up, or when a reset is about to expire. "
             + "It picks the reset that expires soonest, then the account with the most resets, and skips an "
             + "account whose limit resets within 2 h anyway. Runs on \(runsOn) only.")
            .font(.caption2)
            .foregroundStyle(.tertiary)
            .fixedSize(horizontal: false, vertical: true)
        if let last = state.autoResetMachine?.lastReset, let email = last.email {
            Text("Last reset: \(email)" + (last.at.map { " · \(MachineSection.age(since: $0)) ago" } ?? ""))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .help(last.grant ?? "")
        }
    }

    private func detail(_ machine: AutoSwitchMachine) -> String {
        guard machine.ok else { return machine.error ?? "error" }
        guard machine.installed == true else { return "off" }
        guard let at = machine.lastSwitch?.lastSwitchAt else { return "on" }
        return "switched \(MachineSection.age(since: at)) ago"
    }

    private func lastSwitchHelp(_ machine: AutoSwitchMachine) -> String {
        guard let last = machine.lastSwitch, let to = last.to else { return "" }
        return "Last switch: \(last.from ?? "?") → \(to)"
    }
}

private struct SyncSection: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Divider().padding(.vertical, 4)
            HStack(spacing: 6) {
                Image(systemName: "arrow.triangle.2.circlepath")
                    .foregroundStyle(.secondary)
                Text("Shared logins")
                    .font(.callout.weight(.semibold))
                Spacer()
                if let sync = state.sync, sync.enabled {
                    Circle()
                        .fill(sync.reachable == false ? Color.red : (sync.reachable == true ? Color.green : Color.secondary))
                        .frame(width: 6, height: 6)
                    Text(sync.reachable == false ? "hub unreachable" : "hub online")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            if let sync = state.sync, sync.enabled {
                Text(sync.hubUrl ?? "")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let error = sync.error, !error.isEmpty {
                    Text(error)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .lineLimit(2)
                }
                ForEach(machineRows(sync), id: \.name) { row in
                    HStack(spacing: 6) {
                        Text(row.name)
                            .font(.caption)
                        Spacer()
                        Text(row.detail)
                            .font(.caption2)
                            .foregroundStyle(row.problem ? AnyShapeStyle(Color.orange) : AnyShapeStyle(.secondary))
                            .lineLimit(1)
                    }
                    .help(row.errors.joined(separator: "\n"))
                }
                Text("One hub refreshes every shared login; machines only receive and upload copies.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Off. Set up with `ccc sync init` on one always-on machine and `ccc sync join` on the others (see README).")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 4)
    }

    private struct Row {
        let name: String
        let detail: String
        let problem: Bool
        let errors: [String]
    }

    private func machineRows(_ sync: SyncStatus) -> [Row] {
        let hubGens = (sync.accounts ?? [:]).filter { $0.value.shared != false }
            .mapValues { $0.generation ?? 0 }
        return (sync.machines ?? [:]).sorted { $0.key < $1.key }.map { name, info in
            let behind = hubGens.filter { info.generations?[$0.key] != $0.value }.count
            let errors = info.errors ?? []
            var detail = info.lastSync.map { "synced \(MachineSection.age(since: $0)) ago" } ?? "never synced"
            if behind > 0 { detail += " · \(behind) behind" }
            if !errors.isEmpty { detail += " · \(errors.count) error\(errors.count == 1 ? "" : "s")" }
            return Row(name: name + (name == sync.machine ? " (this Mac)" : ""), detail: detail,
                       problem: behind > 0 || !errors.isEmpty, errors: errors)
        }
    }
}
