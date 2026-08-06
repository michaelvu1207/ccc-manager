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

/// The only place that spawns processes. All invocations are strictly
/// serialized (see `serialized`) so concurrent provider refreshes never
/// contend for the same state files.
actor CCCClient {
    static let shared = CCCClient()

    private var cachedCCCPath: String?
    private var tail: Task<Void, Never> = Task {}

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

    func snapshot(refresh: Bool) async throws -> Snapshot {
        #if DEBUG
        // Deterministic, token-safe visual verification without reading live accounts.
        if let path = ProcessInfo.processInfo.environment["CCC_SNAPSHOT_FIXTURE"] {
            let data = try Data(contentsOf: URL(fileURLWithPath: path))
            return try JSONDecoder().decode(Snapshot.self, from: data)
        }
        #endif
        var args = ["snapshot"]
        if !refresh { args.append("--no-refresh") }
        let data = try await runCCC(arguments: args, timeout: refresh ? 120 : 15)
        return try JSONDecoder().decode(Snapshot.self, from: data)
    }

    func switchAccount(provider: String, num: String) async throws {
        _ = try await runCCC(arguments: ["switch", provider, num], timeout: 30)
    }

    func saveCurrentClaudeLogin(num: String) async throws {
        _ = try await runCCC(arguments: ["account", "save", "claude", num], timeout: 30)
    }

    func consumeCodexReset(num: String, creditID: String?) async throws {
        var arguments = ["reset", "codex", num]
        if let creditID { arguments += ["--credit-id", creditID] }
        arguments.append("--confirm")
        _ = try await runCCC(arguments: arguments, timeout: 30)
    }

    // MARK: - Process plumbing

    private func runCCC(arguments: [String], timeout: TimeInterval) async throws -> Data {
        let ccc = try locateCCC()
        let env = environment()
        return try await serialized {
            try await Self.runAsync(
                executable: ccc, arguments: arguments, environment: env, timeout: timeout
            )
        }
    }

    /// Chain operations so only one child process runs at a time, even across
    /// actor suspension points (actors are reentrant at `await`).
    private func serialized<T: Sendable>(_ op: @Sendable @escaping () async throws -> T) async throws -> T {
        let previous = tail
        let task = Task { () throws -> T in
            await previous.value
            return try await op()
        }
        tail = Task { _ = try? await task.value }
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
