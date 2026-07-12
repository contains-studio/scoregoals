import Foundation

// MARK: - UserDefaults keys shared across the app

enum DayloopDefaults {
    /// A user-chosen repo directory *or* engine binary. When set (and valid) it
    /// overrides the compiled-in default repo, so `DAYLOOP_BIN` isn't the only
    /// way to relocate the engine. Persisted from Settings.
    static let enginePathKey = "dayloopEnginePath"
}

// MARK: - Errors surfaced as a status, never thrown to a crash

enum DayloopError: Error, CustomStringConvertible {
    case launch(String)
    case timeout(TimeInterval)
    case nonZeroExit(Int32, String)
    case emptyOutput

    var description: String {
        switch self {
        case .launch(let m):        return "couldn't launch engine: \(m)"
        case .timeout(let t):       return "engine timed out after \(Int(t))s"
        case .nonZeroExit(let c, let m):
            let tail = m.trimmingCharacters(in: .whitespacesAndNewlines)
            return "engine exited \(c)\(tail.isEmpty ? "" : ": \(tail)")"
        case .emptyOutput:          return "engine returned no output"
        }
    }
}

/// Resolves how to invoke the dayloop engine and runs subcommands via `Process`.
///
/// Resolution order:
///   1. `$DAYLOOP_BIN`                        (hard executable override, cwd = repo)
///   2. UserDefaults `enginePathKey`          (a repo dir, or an explicit binary)
///   3. `<repo>/.venv/bin/dayloop`            (console-script launcher, if present)
///   4. `<repo>/.venv/bin/python -m dayloop`  (module fallback)
///
/// All blocking work happens on a background queue supplied by the caller (see
/// StatusStore) or by `runAsync`; the blocking `run`/`runAction` must never be
/// called on the main thread.
struct DayloopClient {
    /// Absolute path to the executable we actually spawn.
    let executable: URL
    /// Fixed leading arguments (e.g. ["-m", "dayloop"] for the python fallback).
    let baseArguments: [String]
    /// cwd for the child — the dayloop repo, so it finds data/ + config.toml.
    let workingDirectory: URL
    /// Optional debug log path (from DAYLOOP_BAR_DEBUG). Every invocation is appended.
    let debugLogPath: String?

    static let defaultRepo = URL(fileURLWithPath: "/Users/contains/projects/dayloop")

    /// Background queue backing `runAsync` so callers never block a thread pool.
    private static let asyncQueue = DispatchQueue(label: "dayloop.action", qos: .userInitiated, attributes: .concurrent)

    /// Build the default client, honouring env + the persisted engine path.
    static func resolveDefault(repo defaultRepo: URL = DayloopClient.defaultRepo,
                               environment: [String: String] = ProcessInfo.processInfo.environment,
                               defaults: UserDefaults = .standard) -> DayloopClient {
        let debug = resolveDebugLog(environment)
        let fm = FileManager.default

        // Determine the repo directory: a valid UserDefaults directory override,
        // else the compiled-in default. A UserDefaults value pointing at an
        // executable file is treated as an explicit binary instead.
        var repo = defaultRepo
        if let custom = defaults.string(forKey: DayloopDefaults.enginePathKey), !custom.isEmpty {
            var isDir: ObjCBool = false
            if fm.fileExists(atPath: custom, isDirectory: &isDir) {
                if isDir.boolValue {
                    repo = URL(fileURLWithPath: custom)
                } else if fm.isExecutableFile(atPath: custom) {
                    return DayloopClient(executable: URL(fileURLWithPath: custom),
                                         baseArguments: [],
                                         workingDirectory: repoGuess(forBinary: custom, fallback: defaultRepo),
                                         debugLogPath: debug)
                }
            }
        }

        // A hard executable override still wins for *what* to run; cwd = repo.
        if let override = environment["DAYLOOP_BIN"], !override.isEmpty {
            return DayloopClient(executable: URL(fileURLWithPath: override),
                                 baseArguments: [],
                                 workingDirectory: repo,
                                 debugLogPath: debug)
        }

        let launcher = repo.appendingPathComponent(".venv/bin/dayloop")
        let python = repo.appendingPathComponent(".venv/bin/python")
        if fm.isExecutableFile(atPath: launcher.path) {
            return DayloopClient(executable: launcher,
                                 baseArguments: [],
                                 workingDirectory: repo,
                                 debugLogPath: debug)
        }
        return DayloopClient(executable: python,
                             baseArguments: ["-m", "dayloop"],
                             workingDirectory: repo,
                             debugLogPath: debug)
    }

    /// Guess the repo for an explicit binary at `<repo>/.venv/bin/<x>` (3 levels up).
    private static func repoGuess(forBinary path: String, fallback: URL) -> URL {
        let url = URL(fileURLWithPath: path)
        let up = url.deletingLastPathComponent()   // .../.venv/bin
            .deletingLastPathComponent()           // .../.venv
            .deletingLastPathComponent()           // .../<repo>
        return up.path.isEmpty || up.path == "/" ? fallback : up
    }

    private static func resolveDebugLog(_ env: [String: String]) -> String? {
        guard let raw = env["DAYLOOP_BAR_DEBUG"], !raw.isEmpty else { return nil }
        if raw == "1" {
            return NSTemporaryDirectory() + "dayloopbar.log"
        }
        return raw
    }

    /// A human-readable description of the resolved invocation (for the UI / logs).
    var invocationDescription: String {
        ([executable.path] + baseArguments).joined(separator: " ")
    }

    // MARK: - Read: `status --json` etc. (non-empty stdout required)

    /// Run `dayloop <args...>` and return raw stdout on success (must be non-empty).
    /// Blocking; call off the main thread.
    func run(_ args: [String], timeout: TimeInterval = 5) -> Result<Data, DayloopError> {
        switch execute(args, timeout: timeout) {
        case .failure(let e):
            return .failure(e)
        case .success(let (status, out, err)):
            if status != 0 {
                return .failure(.nonZeroExit(status, String(data: err, encoding: .utf8) ?? ""))
            }
            if out.isEmpty {
                log("empty output")
                return .failure(.emptyOutput)
            }
            return .success(out)
        }
    }

    // MARK: - Write: actions (exit 0 == success; stdout may be empty)

    /// Run an arbitrary write/action subcommand. Success is a zero exit code;
    /// stdout (returned trimmed) may legitimately be empty. Throws (as a Result
    /// failure) on launch error, timeout, or non-zero exit. Blocking; off-main.
    func runAction(_ args: [String], timeout: TimeInterval = 20) -> Result<String, DayloopError> {
        switch execute(args, timeout: timeout) {
        case .failure(let e):
            return .failure(e)
        case .success(let (status, out, err)):
            if status != 0 {
                return .failure(.nonZeroExit(status, String(data: err, encoding: .utf8) ?? ""))
            }
            let text = String(data: out, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return .success(text)
        }
    }

    /// Async wrapper around `runAction` — runs the child on a background queue and
    /// resumes the caller (e.g. a @MainActor context) when it exits.
    func runAsync(_ args: [String], timeout: TimeInterval = 20) async -> Result<String, DayloopError> {
        await withCheckedContinuation { continuation in
            Self.asyncQueue.async {
                continuation.resume(returning: runAction(args, timeout: timeout))
            }
        }
    }

    // MARK: - Core process runner (shared)

    /// Launch the child, drain both pipes, enforce the timeout. Only fails for
    /// launch/timeout; otherwise returns `(terminationStatus, stdout, stderr)`.
    private func execute(_ args: [String], timeout: TimeInterval) -> Result<(Int32, Data, Data), DayloopError> {
        log("run: \(args.joined(separator: " "))")

        let process = Process()
        process.executableURL = executable
        process.arguments = baseArguments + args
        process.currentDirectoryURL = workingDirectory

        // Inherit env but force UTF-8 so the sparkline / unicode decode cleanly.
        var env = ProcessInfo.processInfo.environment
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        // Drain both pipes concurrently so a full pipe buffer can never deadlock
        // the child, regardless of output size.
        var outData = Data()
        var errData = Data()
        let ioGroup = DispatchGroup()
        let ioQueue = DispatchQueue(label: "dayloop.io", attributes: .concurrent)
        ioQueue.async(group: ioGroup) {
            outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        }
        ioQueue.async(group: ioGroup) {
            errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        }

        do {
            try process.run()
        } catch {
            log("launch failed: \(error.localizedDescription)")
            return .failure(.launch(error.localizedDescription))
        }

        // Enforce the timeout: wait on a separate thread, terminate if it overruns.
        let finished = DispatchSemaphore(value: 0)
        let waitQueue = DispatchQueue(label: "dayloop.wait")
        waitQueue.async {
            process.waitUntilExit()
            finished.signal()
        }

        if finished.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            // Give it a beat to die, then hard-kill if still alive.
            if finished.wait(timeout: .now() + 1) == .timedOut {
                kill(process.processIdentifier, SIGKILL)
            }
            _ = ioGroup.wait(timeout: .now() + 1)
            log("timeout after \(timeout)s")
            return .failure(.timeout(timeout))
        }

        // Process exited; make sure both reads have completed.
        ioGroup.wait()

        let status = process.terminationStatus
        log("exit \(status): out \(outData.count)B err \(errData.count)B")
        return .success((status, outData, errData))
    }

    // Append-only debug log; best-effort, never throws into the caller.
    private func log(_ message: String) {
        guard let path = debugLogPath else { return }
        let stamp = ISO8601DateFormatter().string(from: Date())
        let line = "[\(stamp)] \(invocationDescription) | \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        let url = URL(fileURLWithPath: path)
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
        } else {
            try? data.write(to: url)
        }
    }
}
