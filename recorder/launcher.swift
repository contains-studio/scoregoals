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
//
// Away-aware pause (see docs/PLAN-experience-and-learning.md, "Sensing
// legitimacy"): the recorder must demonstrably stop watching when Michael
// leaves. We stop the child when the screen locks, the machine sleeps, or
// the user is idle past a threshold *and no meeting is in progress* — a
// hands-off call must keep transcribing. We resume on activity, unlock, or
// wake. All state transitions are logged with a reason.

import Foundation
import IOKit
import AppKit
import CoreGraphics

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

// ── Recorder child state ────────────────────────────────────────────────
// All of the following are read/written on the main queue only. The child's
// terminationHandler hops to main before touching any of them, so there is a
// single serialization domain and no locking is required.
var child: Process?
var quitting = false
// `paused` is the away-policy desired state: true means "we do NOT want the
// recorder running right now" (locked / asleep / idle-no-meeting). It gates
// both spawning and the crash-backoff respawn.
var paused = false
// Monotonic id for each spawned child. A child's terminationHandler captures
// its own epoch; if it no longer matches `childEpoch` the handler is stale
// (a newer child has since been spawned) and must not trigger a respawn.
// This is what prevents the pause→terminate / resume→spawn double-spawn race.
var childEpoch = 0
// Condition inputs, updated by the observers below.
var screenLocked = false
var asleep = false
// True from the moment we deliberately terminate a child (pause) until that
// child's terminationHandler runs. While set, a resume must NOT spawn a fresh
// child — the just-terminated one is still exiting and two screenpipe processes
// would fight over the same port/db. The handler owns the successor spawn.
var awaitingTerminate = false
// Serializes the async /health meeting probe so overlapping idle ticks don't
// stack up concurrent checks.
var meetingCheckInFlight = false

// Idle threshold: SCOREGOALS_AWAY_PAUSE_MIN minutes (default 5). 0 disables
// idle-based pausing entirely (lock/sleep pausing still applies).
let awayThresholdSecs: Double = {
    let env = ProcessInfo.processInfo.environment["SCOREGOALS_AWAY_PAUSE_MIN"]
    let mins = env.flatMap { Double($0) } ?? 5.0
    return mins * 60.0
}()
let idlePauseEnabled = awayThresholdSecs > 0

// ── Forward termination so the recorder shuts down cleanly with the app ──
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

// ── Spawn / stop ─────────────────────────────────────────────────────────
func spawn(backoff: TimeInterval) {
    // Never spawn while quitting or paused, and never double-spawn on top of a
    // live child (guards a resume racing a still-pending crash-backoff timer).
    guard !quitting, !paused else { return }
    if let c = child, c.isRunning { return }

    childEpoch += 1
    let myEpoch = childEpoch
    let p = Process()
    p.executableURL = URL(fileURLWithPath: bin)
    p.arguments = ["record"]
    p.standardOutput = log
    p.standardError = log
    p.terminationHandler = { proc in
        // Hop to main so all state access stays single-threaded.
        DispatchQueue.main.async {
            // Stale handler: a newer child was spawned after this one. Ignore —
            // whoever superseded us owns the current lifecycle.
            guard myEpoch == childEpoch else {
                log.line("recorder exited (status \(proc.terminationStatus)); stale epoch \(myEpoch), ignoring")
                return
            }
            // Was this a child we deliberately terminated (pause)? Capture before
            // clearing so the deferred-resume path below can spawn the successor.
            let wasDeliberate = awaitingTerminate
            awaitingTerminate = false
            guard !quitting else { return }
            if paused {
                // We stopped it on purpose (pause), or it happened to die while
                // we already intend to be paused. Either way, do not respawn.
                log.line("recorder stopped (status \(proc.terminationStatus)) — paused, no respawn")
                return
            }
            if wasDeliberate {
                // Paused then resumed before this child finished exiting. Now that
                // it's gone, spawn the single successor — this deferral is what
                // prevents two screenpipe processes racing the same port/db.
                log.line("resume: previous recorder finished exiting, spawning successor")
                spawn(backoff: 2)
                return
            }
            // Unexpected exit while we want to be running → crash backoff.
            let next = min(backoff * 2, 60)
            log.line("recorder exited (status \(proc.terminationStatus)); restarting in \(Int(backoff))s")
            DispatchQueue.main.asyncAfter(deadline: .now() + backoff) {
                spawn(backoff: next)
            }
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

func applyPause(_ reason: String) {
    guard !paused else { return }   // already paused — don't re-log or re-terminate
    paused = true
    log.line("pause: \(reason)")
    // Terminating here fires the child's terminationHandler, which sees
    // `paused == true` and does not respawn. Drop our reference immediately so
    // startChildIfNeeded()'s "already running" guard can't see a dead child.
    if let c = child, c.isRunning {
        awaitingTerminate = true   // the handler owns the next spawn decision
        c.terminate()
    }
    child = nil
}

func startChildIfNeeded() {
    guard !quitting, !paused else { return }
    if awaitingTerminate {
        // A just-terminated child is still exiting; its terminationHandler will
        // spawn the successor once it's gone. Spawning now would put two
        // screenpipe processes on the same port/db at once.
        log.line("resume deferred: previous recorder still exiting")
        return
    }
    if let c = child, c.isRunning { return }
    spawn(backoff: 2)
}

func applyResume(_ reason: String) {
    guard paused else { return }    // only meaningful when we were paused
    paused = false
    log.line("resume: \(reason)")
    startChildIfNeeded()
}

// ── Sensing ──────────────────────────────────────────────────────────────
// System idle time in seconds via IOKit HIDIdleTime (nanoseconds). Returns
// nil if the property can't be read; callers treat nil as "not idle".
func systemIdleSeconds() -> Double? {
    let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOHIDSystem"))
    guard service != 0 else { return nil }
    defer { IOObjectRelease(service) }
    guard let prop = IORegistryEntryCreateCFProperty(service, "HIDIdleTime" as CFString, kCFAllocatorDefault, 0) else {
        return nil
    }
    guard let num = prop.takeRetainedValue() as? NSNumber else { return nil }
    return num.doubleValue / 1_000_000_000.0   // ns → s
}

// Ground-truth screen-lock state straight from the window server, independent of
// the screenIsLocked/screenIsUnlocked DistributedNotifications. Those can be
// missed (a dropped unlock would strand `screenLocked = true` forever, silently
// pausing capture for the rest of the day; a dropped lock would keep watching a
// locked screen). The 30s poll reconciles against this. Returns nil if the
// session dictionary can't be read; true/false otherwise.
func screenIsLockedGroundTruth() -> Bool? {
    guard let dict = CGSessionCopyCurrentDictionary() as? [String: Any] else { return nil }
    if let n = dict["CGSSessionScreenIsLocked"] as? Int { return n != 0 }
    if let b = dict["CGSSessionScreenIsLocked"] as? Bool { return b }
    return false   // key absent => not locked
}

// Meeting probe against the local screenpipe server. Field path verified
// against a live `curl localhost:3030/health` (screenpipe 0.4.25):
//   audio_pipeline.meeting_detected : Bool
// Note: /health returns HTTP 200 even when its JSON body reports a degraded
// status, so we parse the body rather than trusting the status line. If the
// server is unreachable, times out, or the field is missing/non-bool, we
// report NOT in a meeting — i.e. we fail *toward* pausing. Privacy wins: when
// in doubt about whether a call is live, we stop watching rather than keep
// recording an unattended screen.
func checkMeeting(_ completion: @escaping (Bool) -> Void) {
    guard let url = URL(string: "http://localhost:3030/health") else {
        completion(false); return
    }
    var req = URLRequest(url: url, timeoutInterval: 3)
    req.httpMethod = "GET"
    let task = URLSession.shared.dataTask(with: req) { data, _, err in
        var inMeeting = false
        var note: String
        if let err = err {
            note = "unreachable (\(err.localizedDescription)) → assume no meeting"
        } else if let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let audio = obj["audio_pipeline"] as? [String: Any],
               let detected = audio["meeting_detected"] as? Bool {
                inMeeting = detected
                note = "meeting_detected=\(detected)"
            } else {
                note = "field audio_pipeline.meeting_detected missing/non-bool → assume no meeting"
            }
        } else {
            note = "no/non-JSON /health body → assume no meeting"
        }
        DispatchQueue.main.async {
            // Log the probe outcome on every idle pause decision so a silent
            // degradation (field renamed, server flaky) is visible in the log
            // rather than manifesting as mysterious pausing.
            log.line("meeting probe: \(note)")
            completion(inMeeting)
        }
    }
    task.resume()
}

// One-shot startup check: is the meeting_detected field actually present in
// /health? If not, meeting-aware pausing silently degrades to "always pause when
// idle" (it can pause during a hands-off call). Log a clear warning so that's
// observable at launch rather than a mystery later.
func checkMeetingFieldAtStartup() {
    guard let url = URL(string: "http://localhost:3030/health") else { return }
    var req = URLRequest(url: url, timeoutInterval: 3)
    req.httpMethod = "GET"
    URLSession.shared.dataTask(with: req) { data, _, err in
        DispatchQueue.main.async {
            if let err = err {
                log.line("startup meeting-field check: /health unreachable (\(err.localizedDescription)) — meeting-aware pausing will assume no meeting until screenpipe is up")
                return
            }
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                log.line("startup meeting-field check: /health returned no/non-JSON body — cannot confirm audio_pipeline.meeting_detected")
                return
            }
            if let audio = obj["audio_pipeline"] as? [String: Any], audio["meeting_detected"] is Bool {
                log.line("startup meeting-field check: audio_pipeline.meeting_detected present — meeting-aware idle pausing active")
            } else {
                log.line("startup meeting-field check: WARNING audio_pipeline.meeting_detected ABSENT/renamed in /health — idle pausing cannot detect live meetings and may pause during hands-off calls")
            }
        }
    }.resume()
}

func fmtMin(_ secs: Double) -> String { String(format: "%.1fm", secs / 60.0) }

// ── Policy ────────────────────────────────────────────────────────────────
// Single decision point. Called by every input: lock/unlock, sleep/wake, and
// the 30s idle poll. Hard conditions (lock, sleep) pause immediately and win
// over the meeting exception. Idle is soft: it only pauses when no meeting is
// detected, and it needs an async /health probe to decide.
func reevaluate(_ trigger: String) {
    if screenLocked { applyPause("locked"); return }
    if asleep { applyPause("asleep"); return }

    guard idlePauseEnabled else {
        // Idle pausing is off; only lock/sleep can pause. If we're paused, that
        // was lock/sleep and this trigger (unlock/wake) clears it.
        applyResume(trigger)
        return
    }

    let idle = systemIdleSeconds() ?? 0
    if idle > awayThresholdSecs {
        guard !meetingCheckInFlight else { return }
        meetingCheckInFlight = true
        checkMeeting { inMeeting in
            meetingCheckInFlight = false
            // Conditions may have changed during the async probe. If a hard
            // condition took over, its own handler owns the state — bail.
            if screenLocked || asleep { return }
            let idleNow = systemIdleSeconds() ?? 0
            if idleNow > awayThresholdSecs && !inMeeting {
                applyPause("idle \(fmtMin(idleNow)) no meeting")
            } else {
                // Either activity returned, or a meeting is live — keep recording.
                applyResume(inMeeting ? "meeting active" : "activity")
            }
        }
    } else {
        applyResume("activity")
    }
}

// ── Observers ──────────────────────────────────────────────────────────────
let dnc = DistributedNotificationCenter.default()
dnc.addObserver(forName: NSNotification.Name("com.apple.screenIsLocked"),
                object: nil, queue: .main) { _ in
    screenLocked = true
    reevaluate("lock")
}
dnc.addObserver(forName: NSNotification.Name("com.apple.screenIsUnlocked"),
                object: nil, queue: .main) { _ in
    screenLocked = false
    reevaluate("unlock")
}

let wsnc = NSWorkspace.shared.notificationCenter
wsnc.addObserver(forName: NSWorkspace.willSleepNotification,
                 object: nil, queue: .main) { _ in
    asleep = true
    reevaluate("sleep")
}
wsnc.addObserver(forName: NSWorkspace.didWakeNotification,
                 object: nil, queue: .main) { _ in
    asleep = false
    reevaluate("wake")
}

// 30s poll. Two jobs: (1) reconcile the lock flag against window-server ground
// truth so a missed lock/unlock notification can't strand us paused-forever or
// watching-through-a-lock; (2) catch "user left" (→ pause) / "user returned"
// (→ resume) for idle. Runs regardless of idlePauseEnabled — the lock
// reconciliation must happen even when idle-based pausing is off (lock/sleep
// pausing still applies), and reevaluate() is a no-op resume when nothing is
// wrong.
let idleTimer = DispatchSource.makeTimerSource(queue: .main)
idleTimer.schedule(deadline: .now() + 30, repeating: 30)
idleTimer.setEventHandler {
    if let truth = screenIsLockedGroundTruth(), truth != screenLocked {
        log.line("lock flag corrected \(screenLocked) → \(truth) via CGSession ground truth (missed notification)")
        screenLocked = truth
    }
    reevaluate("idle-poll")
}
idleTimer.resume()

log.line("away-pause: idle threshold \(idlePauseEnabled ? "\(fmtMin(awayThresholdSecs))" : "disabled"), lock/sleep watch active, lock ground-truth poll active")
checkMeetingFieldAtStartup()

spawn(backoff: 2)
RunLoop.main.run()
