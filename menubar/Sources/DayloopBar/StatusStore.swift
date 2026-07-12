import Foundation
import Combine
import SwiftUI
import AppKit

/// Overall health of the icon/UI, derived from the latest poll.
enum BarState {
    case onTrack      // score present, on_track == true      -> green
    case drifting     // score present, on_track == false     -> amber
    case off          // engine error / no data               -> red
    case loading      // first fetch in flight                -> greyed

    var tint: Color {
        switch self {
        case .onTrack:  return .green
        case .drifting: return .orange
        case .off:      return .red
        case .loading:  return .secondary
        }
    }
}

/// A transient one-line result from a write action (success or failure), shown
/// inline under the quick-actions row.
struct ActionMessage: Equatable {
    var text: String
    var isError: Bool
}

/// Polls `dayloop status --json` on a (live-adjustable) cadence, decodes the
/// snapshot, and runs write/action subcommands off the main thread, refreshing
/// the snapshot after each successful write.
@MainActor
final class StatusStore: ObservableObject {
    @Published private(set) var status: DayloopStatus? = nil
    @Published private(set) var lastError: String? = nil
    @Published private(set) var lastUpdated: Date? = nil
    @Published private(set) var isRefreshing: Bool = false

    /// Effective app settings from `config --json` (populated on start + Settings open).
    @Published private(set) var config: DayloopConfig? = nil
    /// Whether a Gemini API key is stored (BYOK). Loaded via `config get
    /// gemini_api_key`, which prints only "set"/"not set" — the key value is
    /// never read into the app.
    @Published private(set) var geminiKeyIsSet: Bool = false
    /// Keys of write actions currently in flight (e.g. "today", "focus", "capture").
    @Published private(set) var busyActions: Set<String> = []
    /// The last write action's result, shown inline; auto-clears on the next action.
    @Published var actionMessage: ActionMessage? = nil

    /// Poll cadence in seconds; live-adjustable from Settings.
    @Published private(set) var refreshSeconds: TimeInterval

    private var client: DayloopClient
    private let workQueue = DispatchQueue(label: "dayloop.status.poll", qos: .userInitiated)
    private var timer: Timer?

    init(client: DayloopClient = .resolveDefault(),
         refreshSeconds: TimeInterval = StatusStore.resolvedRefreshSeconds()) {
        self.client = client
        self.refreshSeconds = refreshSeconds
    }

    nonisolated static func resolvedRefreshSeconds() -> TimeInterval {
        if let raw = ProcessInfo.processInfo.environment["DAYLOOP_REFRESH_SECONDS"],
           let n = Double(raw), n >= 1 {
            return n
        }
        return 30
    }

    /// A description of the resolved engine invocation (shown in Settings / logs).
    var engineInvocation: String { client.invocationDescription }
    /// False when no dayloop engine could be located — the UI shows a clear
    /// "engine not found — set path in Settings" hint rather than an opaque error.
    var engineResolved: Bool { client.isResolved }
    /// The repo the engine runs in — used to open goals.md / reveal reports.
    var repoURL: URL { client.workingDirectory }
    /// The day the current snapshot summarizes (fallback: today, local).
    var todayDate: String { status?.date ?? Self.isoDay(Date()) }

    // MARK: - Lifecycle

    func start() {
        guard timer == nil else { return }   // idempotent: safe from multiple onAppear
        refresh()
        loadConfig()
        scheduleTimer()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    private func scheduleTimer() {
        timer?.invalidate()
        let t = Timer(timeInterval: refreshSeconds, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    // MARK: - Fetch (status --json)

    func refresh() {
        guard !isRefreshing else { return }
        isRefreshing = true
        let client = self.client
        workQueue.async { [weak self] in
            let result = client.run(["status", "--json"], timeout: 5)
            var decodedStatus: DayloopStatus? = nil
            var errorMessage: String? = nil
            switch result {
            case .success(let data):
                do {
                    decodedStatus = try JSONDecoder().decode(DayloopStatus.self, from: data)
                } catch {
                    errorMessage = "couldn't decode status JSON: \(error)"
                }
            case .failure(let err):
                errorMessage = err.description
            }
            Task { @MainActor in
                guard let self else { return }
                self.isRefreshing = false
                if let s = decodedStatus {
                    self.status = s
                    self.lastError = nil
                    self.lastUpdated = Date()
                } else {
                    // Keep the last good `status` visible but flag the error.
                    self.lastError = errorMessage ?? "unknown error"
                }
            }
        }
    }

    /// Load `config --json` into `self.config` and sync the live poll cadence.
    func loadConfig() {
        Task {
            let result = await client.runAsync(["config", "--json"], timeout: 8)
            if case .success(let text) = result,
               let data = text.data(using: .utf8),
               let cfg = try? JSONDecoder().decode(DayloopConfig.self, from: data) {
                self.config = cfg
                self.applyRefreshCadence(TimeInterval(cfg.refreshSeconds))
            }
        }
    }

    // MARK: - Write actions

    /// Run a write action off-main; on success optionally run `onSuccess`, set a
    /// notice, and re-poll the engine. `key` de-dupes concurrent presses + drives
    /// per-button spinners.
    func perform(_ key: String,
                 _ args: [String],
                 timeout: TimeInterval = 20,
                 notice: String? = nil,
                 refreshAfter: Bool = true,
                 onSuccess: ((String) -> Void)? = nil) {
        guard !busyActions.contains(key) else { return }
        busyActions.insert(key)
        actionMessage = nil
        Task {
            let result = await client.runAsync(args, timeout: timeout)
            self.busyActions.remove(key)
            switch result {
            case .success(let output):
                onSuccess?(output)
                if self.actionMessage == nil {
                    let text = notice ?? output.split(separator: "\n").first.map(String.init) ?? "done"
                    self.actionMessage = ActionMessage(text: text, isError: false)
                }
                if refreshAfter { self.refresh() }
            case .failure(let err):
                self.actionMessage = ActionMessage(text: err.description, isError: true)
            }
        }
    }

    // Intentions ---------------------------------------------------------------

    func toggleIntention(_ id: String) {
        perform("today", ["today", "toggle", id])
    }

    /// `parts` are already-trimmed non-empty intention strings (up to 3).
    func setIntentions(_ parts: [String]) {
        let joined = parts.prefix(3).joined(separator: "|")
        perform("today", ["today", "set", joined], notice: "today's 3 set")
    }

    // Focus --------------------------------------------------------------------

    func startFocus(goalId: String, minutes: Int) {
        perform("focus", ["focus", "start", goalId, "--minutes", "\(minutes)"])
    }

    func stopFocus() {
        perform("focus", ["focus", "stop"])
    }

    // Quick actions ------------------------------------------------------------

    func captureNow() {
        let date = todayDate
        perform("capture", ["capture", date], timeout: 60, notice: "captured \(date)")
    }

    /// Generate the EOD report, then reveal the produced markdown in Finder.
    func generateReport() {
        let date = todayDate
        // `report --backend` accepts only ollama|gemini; map "both" -> ollama.
        let backend = (status?.health.backend.defaultBackend == "gemini") ? "gemini" : "ollama"
        perform("report", ["report", date, "--backend", backend], timeout: 180,
                notice: "EOD report ready") { [weak self] _ in
            self?.revealReport(date: date)
        }
    }

    func planDay() {
        perform("plan", ["plan"], timeout: 60, notice: "plan generated")
    }

    // Config -------------------------------------------------------------------

    func setConfig(_ key: String, _ value: String) {
        perform("config", ["config", "set", key, value], notice: "\(key) = \(value)") { [weak self] _ in
            self?.loadConfig()
        }
    }

    /// Load whether a Gemini API key is stored (prints "set"/"not set" only).
    func loadGeminiKeyState() {
        Task {
            let result = await client.runAsync(["config", "get", "gemini_api_key"], timeout: 8)
            if case .success(let text) = result {
                self.geminiKeyIsSet =
                    text.trimmingCharacters(in: .whitespacesAndNewlines) == "set"
            }
        }
    }

    /// Save (or, with an empty string, clear) the BYOK Gemini API key. The value
    /// is passed straight to the engine and is never placed in a notice or log —
    /// only the resulting "set/not set" state is surfaced.
    func setGeminiKey(_ key: String) {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        perform("config", ["config", "set", "gemini_api_key", trimmed],
                notice: trimmed.isEmpty ? "Gemini key cleared" : "Gemini key saved",
                refreshAfter: false) { [weak self] _ in
            self?.loadGeminiKeyState()
        }
    }

    /// Write `refresh_seconds` and update the live poll cadence immediately.
    func setRefreshSeconds(_ n: Int) {
        perform("config", ["config", "set", "refresh_seconds", "\(n)"],
                notice: "refresh = \(n)s") { [weak self] _ in
            self?.applyRefreshCadence(TimeInterval(n))
            self?.loadConfig()
        }
    }

    private func applyRefreshCadence(_ n: TimeInterval) {
        guard n >= 1, n != refreshSeconds else { return }
        refreshSeconds = n
        if timer != nil { scheduleTimer() }   // reschedule at the new cadence
    }

    // Engine path (UserDefaults) -----------------------------------------------

    /// Persist a new repo/binary path, rebuild the client, and re-poll.
    func setEnginePath(_ path: String) {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            UserDefaults.standard.removeObject(forKey: DayloopDefaults.enginePathKey)
        } else {
            UserDefaults.standard.set(trimmed, forKey: DayloopDefaults.enginePathKey)
        }
        client = .resolveDefault()
        actionMessage = ActionMessage(text: "engine → \(client.invocationDescription)", isError: false)
        refresh()
        loadConfig()
    }

    func openGoalsFile() {
        let url = repoURL.appendingPathComponent("goals.md")
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.open(url)
        } else {
            actionMessage = ActionMessage(text: "goals.md not found at \(url.path)", isError: true)
        }
    }

    private func revealReport(date: String) {
        let url = repoURL.appendingPathComponent("data/reports/\(date)-eod.md")
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            actionMessage = ActionMessage(text: "report ran but \(url.lastPathComponent) not found", isError: true)
        }
    }

    // MARK: - Derived UI state

    /// True when the freshest data is older than ~2 poll cycles, or an error stands.
    var isStale: Bool {
        if lastError != nil { return true }
        guard let updated = lastUpdated else { return true }
        return Date().timeIntervalSince(updated) > refreshSeconds * 2 + 5
    }

    var barState: BarState {
        if lastError != nil, status == nil { return .off }
        guard let s = status else { return .loading }
        if lastError != nil { return .off }
        return s.score.onTrack ? .onTrack : .drifting
    }

    /// The number shown in the menu bar, or "--" when we have nothing.
    var scoreText: String {
        guard let s = status else { return "--" }
        return "\(s.score.overall)"
    }

    // MARK: - Helpers

    static func isoDay(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }

    /// Parse an ISO-8601 string, tolerating both offset and naive-local forms.
    static func parseISO(_ s: String) -> Date? {
        let withOffset = ISO8601DateFormatter()
        withOffset.formatOptions = [.withInternetDateTime]
        if let d = withOffset.date(from: s) { return d }
        // Naive local (no offset), e.g. "2026-07-11T18:30:00".
        let naive = DateFormatter()
        naive.locale = Locale(identifier: "en_US_POSIX")
        naive.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return naive.date(from: s)
    }
}
