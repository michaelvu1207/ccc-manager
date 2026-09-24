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
                    TextField("Name", text: $draft)
                        .textFieldStyle(.roundedBorder)
                        .controlSize(.small)
                        .focused($nameFocused)
                        .onSubmit(commit)
                        .onExitCommand { editing = false }
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
                .help(editing ? "Save name" : "Rename")

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
        editing = true
        nameFocused = true
    }

    private func commit() {
        editing = false
        Task { await state.renameMachine(machine, to: draft) }
    }
}
