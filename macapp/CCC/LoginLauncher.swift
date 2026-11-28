import AppKit

/// Interactive flows (`claude /login`, browser logins, adding an account on
/// another machine over `ssh -t`) run in Terminal.app. A temp `.command` file
/// avoids the Apple-Events automation permission that scripting Terminal
/// would require.
enum LoginLauncher {
    /// Re-login one account. Claude needs the slot; codex picks the account in the browser.
    static func openLogin(machine: String, provider: String, num: String?) throws {
        var args = ["login", provider, "--machine", machine]
        if let num { args += ["--", num] }
        try openTerminal(cccArguments: args, name: "ccc-login")
    }

    /// One-button login on this Mac, shared through the sync hub
    /// (`ccc sync login`). With an email it re-logs that account.
    static func openSyncLogin(email: String?) throws {
        try openTerminal(cccArguments: syncLoginArguments(email: email), name: "ccc-sync-login")
    }

    static func syncLoginArguments(email: String?) -> [String] {
        var args = ["sync", "login"]
        if let email { args += ["--email", email] }
        return args
    }

    /// Proxy mode: add or renew a login in the proxy (`ccc proxy login`). The
    /// browser opens on this Mac; over SSH the callback goes through a tunnel.
    static func openProxyLogin(provider: String) throws {
        try openTerminal(cccArguments: proxyLoginArguments(provider: provider), name: "ccc-proxy-login")
    }

    static func proxyLoginArguments(provider: String) -> [String] {
        ["proxy", "login", provider]
    }

    /// Log in a new Claude account on a machine, then `cswap add` it.
    static func openAddAccount(machine: String) throws {
        try openTerminal(cccArguments: ["add-account", "--machine", machine], name: "ccc-add-account")
    }

    private static func openTerminal(cccArguments: [String], name: String) throws {
        let script = """
        #!/bin/zsh
        export PATH=\(shellQuote(CCCClient.augmentedPATH()))
        clear
        ccc \(cccArguments.map(shellQuote).joined(separator: " "))
        echo
        echo "done — you can close this window"
        """
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(name)-\(UUID().uuidString.prefix(8)).command")
        try script.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: url.path
        )
        let terminal = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")
        NSWorkspace.shared.open(
            [url], withApplicationAt: terminal,
            configuration: NSWorkspace.OpenConfiguration()
        )
    }

    /// POSIX single-quote an argument for the zsh script.
    static func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
