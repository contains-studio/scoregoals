import Foundation

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
/// Resolution order (matches the brief):
///   1. `<repo>/.venv/bin/dayloop`            (console-script launcher, if present)
///   2. `<repo>/.venv/bin/python -m dayloop`  (module fallback)
///
/// All work happens on a background queue supplied by the caller (see StatusStore);
/// `run` itself blocks the calling thread until exit/timeout, so it must never be
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

    /// Build the default client by probing the venv for the console-script launcher.
    static func resolveDefault(repo: URL = defaultRepo,
                               environment: [String: String] = ProcessInfo.processInfo.environment) -> DayloopClient {
        let launcher = repo.appendingPathComponent(".venv/bin/dayloop")
        let python = repo.appendingPathComponent(".venv/bin/python")

        // Allow a hard override for the executable via env (used later by settings).
        if let override = environment["DAYLOOP_BIN"], !override.isEmpty {
            return DayloopClient(executable: URL(fileURLWithPath: override),
                                 baseArguments: [],
                                 workingDirectory: repo,
                                 debugLogPath: Self.resolveDebugLog(environment))
        }

        let fm = FileManager.default
        if fm.isExecutableFile(atPath: launcher.path) {
            return DayloopClient(executable: launcher,
                                 baseArguments: [],
                                 workingDirectory: repo,
                                 debugLogPath: Self.resolveDebugLog(environment))
        }
        return DayloopClient(executable: python,
                             baseArguments: ["-m", "dayloop"],
                             workingDirectory: repo,
                             debugLogPath: Self.resolveDebugLog(environment))
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

    /// Run `dayloop <args...>` and return raw stdout on success.
    /// Blocking; call off the main thread. Enforces `timeout` by terminating the child.
    func run(_ args: [String], timeout: TimeInterval = 5) -> Result<Data, DayloopError> {
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

        // Drain both pipes concurrently so a full pipe buffer can never deadlock the
        // child, regardless of output size.
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
        if status != 0 {
            let msg = String(data: errData, encoding: .utf8) ?? ""
            log("exit \(status): \(msg.prefix(200))")
            return .failure(.nonZeroExit(status, msg))
        }
        if outData.isEmpty {
            log("empty output")
            return .failure(.emptyOutput)
        }
        log("ok: \(outData.count) bytes")
        return .success(outData)
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
