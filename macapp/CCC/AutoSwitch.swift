import Foundation

/// In-app auto-switch. When the account a machine uses has 5% or less left
/// in its 5-hour or weekly window, that machine switches to the first account
/// in list order (the weekly quota that resets soonest comes first) that is
/// stored there, works, and has more than 5% left. Accounts the user excludes
/// get no number and are never a target. Pure, so tests drive it with a view.
enum AutoSwitch {
    /// Switch away at this many percent left, or fewer, in either window.
    static let lowPercentLeft: Double = 5

    struct Move: Equatable {
        let machine: String
        let provider: String
        let from: String
        let to: String
        /// Slot on that machine (pending-spinner key).
        let num: String
        /// What `ccc switch` takes there: the slot for Claude, the email for Codex.
        let query: String
        let fromLeft: Double
    }

    /// Auto-switch order per provider: 1, 2, 3… top to bottom, skipping
    /// excluded accounts. Excluded accounts have no entry.
    static func order(_ accounts: [ViewAccount], excluded: Set<String>) -> [String: Int] {
        var numbers: [String: Int] = [:]
        for provider in ["claude", "codex"] {
            let included = accounts.filter { $0.provider == provider && !excluded.contains($0.key) }
            for (index, account) in included.enumerated() { numbers[account.key] = index + 1 }
        }
        return numbers
    }

    /// Least percent left across the 5-hour and weekly windows; nil if neither is known.
    static func percentLeft(_ account: ViewAccount) -> Double? {
        account.windows.filter { $0.id == "5h" || $0.id == "wk" }.compactMap(\.pctLeft).min()
    }

    static func isLow(_ account: ViewAccount) -> Bool {
        percentLeft(account).map { $0 <= lowPercentLeft } ?? false
    }

    /// One move per online machine and provider whose active account is low.
    static func plan(_ view: AccountsView, excluded: Set<String>) -> [Move] {
        let numbers = order(view.accounts, excluded: excluded)
        var moves: [Move] = []
        for machine in view.machines where machine.online {
            for provider in ["claude", "codex"] {
                let accounts = view.accounts.filter { $0.provider == provider }
                guard let active = accounts.first(where: { $0.presence(on: machine.id)?.state == .active }),
                      let left = percentLeft(active), left <= lowPercentLeft else { continue }
                let target = accounts.lazy.compactMap { account -> Move? in
                    guard numbers[account.key] != nil, !account.needsLogin, account.hubDeleted != true,
                          let presence = account.presence(on: machine.id), presence.state == .stored,
                          !presence.isDisabled, let num = presence.num, let query = presence.query,
                          let targetLeft = percentLeft(account), targetLeft > lowPercentLeft
                    else { return nil }
                    return Move(machine: machine.id, provider: provider, from: active.email,
                                to: account.email, num: num, query: query, fromLeft: left)
                }.first
                if let target { moves.append(target) }
            }
        }
        return moves
    }
}
