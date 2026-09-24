import Foundation

enum CCCError: LocalizedError {
    case cccNotFound
    case commandFailed(String)
    case timedOut

    var errorDescription: String? {
        switch self {
        case .cccNotFound:
            return "ccc not found — install with `uv tool install` from the CCCSwitcher repo"
        case .commandFailed(let detail):
            return detail
        case .timedOut:
            return "ccc timed out"
        }
    }
}

/// The only place that spawns processes. Invocations are serialized per
/// machine (see `serialized`) so two runs never contend for one machine's
/// state files, while different machines load in parallel.
actor CCCClient {
    static let shared = CCCClient()

    private var cachedCCCPath: String?
    private var tails: [String: Task<Void, Never>] = [:]

    // MARK: - Environment

    /// GUI apps inherit a bare PATH (/usr/bin:/bin:...). The companion `ccc`
    /// executable commonly lives in a user-level bin, so augment PATH.
    static func augmentedPATH() -> String {
        let home = NSHomeDirectory()
        let paths = [
            "\(home)/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
        let base = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        return (paths + [base]).joined(separator: ":")
    }

    private func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = Self.augmentedPATH()
        return env
    }

    // MARK: - Locating ccc

    func locateCCC() throws -> String {
        if let cached = cachedCCCPath { return cached }
        if let override = UserDefaults.standard.string(forKey: "cccPath"),
           FileManager.default.isExecutableFile(atPath: override) {
            cachedCCCPath = override
            return override
        }
        let home = NSHomeDirectory()
        let candidates = [
            "\(home)/.local/bin/ccc",
            "/opt/homebrew/bin/ccc",
            "/usr/local/bin/ccc",
        ]
        if let found = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) {
            cachedCCCPath = found
            return found
        }
        // Last resort: ask a login shell, which has the user's real PATH.
        if let data = try? Self.runBlocking(
            executable: "/bin/zsh",
            arguments: ["-l", "-c", "command -v ccc"],
            environment: ProcessInfo.processInfo.environment,
            timeout: 10
        ), let path = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !path.isEmpty {
            cachedCCCPath = path
            return path
        }
        throw CCCError.cccNotFound
    }

    // MARK: - Public API

    /// Account state. `machines` limits the load to those ids (a remote probe
    /// never waits for this Mac and vice versa); nil means every enabled
    /// machine. Cached reads (`refresh: false`) never wait on running probes.
    func snapshot(refresh: Bool, machines: [String]? = nil) async throws -> Snapshot {
        #if DEBUG
        // Deterministic, token-safe visual verification without reading live accounts.
        if let path = ProcessInfo.processInfo.environment["CCC_SNAPSHOT_FIXTURE"] {
            let data = try Data(contentsOf: URL(fileURLWithPath: path))
            return try JSONDecoder().decode(Snapshot.self, from: data)
        }
        #endif
        var args = ["snapshot"]
        if !refresh { args.append("--no-refresh") }
        for id in machines ?? [] { args += ["--machine", id] }
        let key = refresh ? (machines?.joined(separator: ",") ?? "*") : nil
        // ccc bounds each remote probe itself (ConnectTimeout + CCC_REMOTE_TIMEOUT).
        let data = try await runCCC(arguments: args, timeout: refresh ? 150 : 20, key: key)
        return try JSONDecoder().decode(Snapshot.self, from: data)
    }

    func switchAccount(machine: String, provider: String, account: String) async throws {
        _ = try await runCCC(
            arguments: ["switch", provider, "--machine", machine, "--", account],
            timeout: 90, key: machine
        )
    }

    /// Claude only: hold out of / return to auto-rotation.
    func setDisabled(_ disabled: Bool, machine: String, account: String) async throws {
        _ = try await runCCC(
            arguments: [disabled ? "disable" : "enable", "--machine", machine, "--", account],
            timeout: 90, key: machine
        )
    }

    /// The UI has already asked for confirmation, hence `--yes`.
    func removeAccount(machine: String, provider: String, account: String) async throws {
        _ = try await runCCC(
            arguments: ["account", "remove", provider, "--machine", machine, "--yes", "--", account],
            timeout: 90, key: machine
        )
    }

    func saveCurrentClaudeLogin(num: String) async throws {
        _ = try await runCCC(arguments: ["account", "save", "claude", num], timeout: 30, key: "local")
    }

    func consumeCodexReset(num: String, creditID: String?) async throws {
        var arguments = ["reset", "codex", num]
        if let creditID { arguments += ["--credit-id", creditID] }
        arguments.append("--confirm")
        _ = try await runCCC(arguments: arguments, timeout: 30, key: "local")
    }

    func machineList() async throws -> MachineList {
        let data = try await runCCC(arguments: ["machines", "list", "--json"], timeout: 15, key: "config")
        return try JSONDecoder().decode(MachineList.self, from: data)
    }

    /// `ccc machines <args>` (add / remove / rename / enable / disable / import-starline).
    func editMachines(_ args: [String]) async throws {
        _ = try await runCCC(arguments: ["machines"] + args, timeout: 15, key: "config")
    }

    // MARK: - Process plumbing

    /// Commands that share a `key` (one machine, or the config file) run one
    /// at a time so two runs never race on the same machine's state files.
    /// Different machines run in parallel; `key: nil` (cached reads) never waits.
    private func runCCC(arguments: [String], timeout: TimeInterval, key: String?) async throws -> Data {
        let ccc = try locateCCC()
        let env = environment()
        let op: @Sendable () async throws -> Data = {
            try await Self.runAsync(
                executable: ccc, arguments: arguments, environment: env, timeout: timeout
            )
        }
        guard let key else { return try await op() }
        return try await serialized(key: key, op)
    }

    /// Chain operations per key so only one child process per key runs at a
    /// time, even across actor suspension points (actors are reentrant at `await`).
    private func serialized<T: Sendable>(
        key: String, _ op: @Sendable @escaping () async throws -> T
    ) async throws -> T {
        let previous = tails[key] ?? Task {}
        let task = Task { () throws -> T in
            await previous.value
            return try await op()
        }
        tails[key] = Task { _ = try? await task.value }
        return try await task.value
    }

    private static func runAsync(
        executable: String, arguments: [String], environment: [String: String], timeout: TimeInterval
    ) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let data = try runBlocking(
                        executable: executable, arguments: arguments,
                        environment: environment, timeout: timeout
                    )
                    continuation.resume(returning: data)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private static func runBlocking(
        executable: String, arguments: [String], environment: [String: String], timeout: TimeInterval
    ) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = environment
        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        process.standardInput = FileHandle.nullDevice
        try process.run()

        var timedOut = false
        let watchdog = DispatchWorkItem {
            if process.isRunning {
                timedOut = true
                process.terminate()
            }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)

        // Read before waiting so a large output can't deadlock the pipe.
        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        let errData = stderr.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        watchdog.cancel()

        if timedOut { throw CCCError.timedOut }
        guard process.terminationStatus == 0 else {
            let detail = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw CCCError.commandFailed(
                detail?.isEmpty == false ? detail! : "exit \(process.terminationStatus)"
            )
        }
        return outData
    }
}
