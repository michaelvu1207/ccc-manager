import AppKit

/// Re-login is interactive (`claude /login` / browser flow), so it runs in
/// Terminal.app. A temp `.command` file avoids the Apple-Events automation
/// permission that scripting Terminal would require.
enum LoginLauncher {
    static func openLogin(provider: String, num: String?) throws {
        let cccArgs = ["login", provider] + (num.map { [$0] } ?? [])
        let script = """
        #!/bin/zsh
        export PATH="\(CCCClient.augmentedPATH())"
        clear
        ccc \(cccArgs.joined(separator: " "))
        echo
        echo "done — you can close this window"
        """
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("ccc-login.command")
        try script.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: url.path
        )
        let terminal = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")
        NSWorkspace.shared.open(
            [url], withApplicationAt: terminal,
            configuration: NSWorkspace.OpenConfiguration()
        )
    }
}
