// ScreenpipeRecorder — minimal .app launcher for the screenpipe CLI.
//
// Why this exists: macOS TCC grants Screen Recording to an app *bundle*
// identity. A bare `screenpipe record` CLI gets attributed to whatever
// launched it (Terminal, claude, launchd), so the grant is misplaced and
// fragile. This wrapper is a stable, signable identity: grant Screen
// Recording + Microphone to "ScreenpipeRecorder" once and it sticks, no
// matter how or when the recorder starts.
//
// Behavior: locate the screenpipe CLI, spawn `screenpipe record`, append
// its output to ~/Library/Logs/screenpipe-recorder.log, restart it with
// backoff if it dies, and terminate the child cleanly when the app quits.

import Foundation

let log = FileHandle.forLogging()

func findScreenpipe() -> String? {
    if let env = ProcessInfo.processInfo.environment["SCREENPIPE_BIN"],
       !env.isEmpty, FileManager.default.isExecutableFile(atPath: env) {
        return env
    }
    let home = NSHomeDirectory()
    let candidates = [
        "\(home)/.local/bin/screenpipe",
        "/opt/homebrew/bin/screenpipe",
        "/usr/local/bin/screenpipe",
    ]
    for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
        return c
    }
    // Last resort: whatever a login shell would find.
    let probe = Process()
    probe.executableURL = URL(fileURLWithPath: "/bin/zsh")
    probe.arguments = ["-lc", "command -v screenpipe"]
    let pipe = Pipe()
    probe.standardOutput = pipe
    try? probe.run()
    probe.waitUntilExit()
    let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                     encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
    if let out, !out.isEmpty, FileManager.default.isExecutableFile(atPath: out) {
        return out
    }
    return nil
}

extension FileHandle {
    static func forLogging() -> FileHandle {
        let dir = NSHomeDirectory() + "/Library/Logs"
        let path = dir + "/screenpipe-recorder.log"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        let h = FileHandle(forWritingAtPath: path) ?? .standardError
        h.seekToEndOfFile()
        return h
    }
    func line(_ s: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        write(Data("[\(ts)] [wrapper] \(s)\n".utf8))
    }
}

guard let bin = findScreenpipe() else {
    log.line("screenpipe CLI not found — install with: npm i -g screenpipe")
    exit(1)
}
log.line("using screenpipe at \(bin)")

var child: Process?
var quitting = false

// Forward termination so the recorder shuts down cleanly with the app.
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        quitting = true
        log.line("received signal \(sig), stopping recorder")
        child?.terminate()
        // Give the child a moment to flush, then exit.
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { exit(0) }
    }
    src.resume()
    // Keep sources alive for the lifetime of the process.
    _ = Unmanaged.passRetained(src as AnyObject)
}

func spawn(backoff: TimeInterval) {
    guard !quitting else { return }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: bin)
    p.arguments = ["record"]
    p.standardOutput = log
    p.standardError = log
    p.terminationHandler = { proc in
        guard !quitting else { return }
        let next = min(backoff * 2, 60)
        log.line("recorder exited (status \(proc.terminationStatus)); restarting in \(Int(backoff))s")
        DispatchQueue.main.asyncAfter(deadline: .now() + backoff) {
            spawn(backoff: next)
        }
    }
    do {
        try p.run()
        child = p
        log.line("recorder started (pid \(p.processIdentifier))")
    } catch {
        let next = min(backoff * 2, 60)
        log.line("failed to start recorder: \(error); retrying in \(Int(backoff))s")
        DispatchQueue.main.asyncAfter(deadline: .now() + backoff) {
            spawn(backoff: next)
        }
    }
}

spawn(backoff: 2)
RunLoop.main.run()
