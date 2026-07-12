import Foundation
import Combine
import SwiftUI

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

/// Polls `dayloop status --json` on a fixed cadence and publishes the decoded
/// snapshot (or a human-readable error) for the SwiftUI views.
@MainActor
final class StatusStore: ObservableObject {
    @Published private(set) var status: DayloopStatus? = nil
    @Published private(set) var lastError: String? = nil
    @Published private(set) var lastUpdated: Date? = nil
    @Published private(set) var isRefreshing: Bool = false

    /// Poll cadence in seconds (advisory: `config.refresh_seconds` default 30).
    let refreshSeconds: TimeInterval

    private let client: DayloopClient
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

    /// A description of the resolved engine invocation (shown in the footer / logs).
    var engineInvocation: String { client.invocationDescription }

    // MARK: - Lifecycle

    func start() {
        guard timer == nil else { return }   // idempotent: safe to call from multiple onAppear
        refresh()
        let t = Timer(timeInterval: refreshSeconds, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    // MARK: - Fetch

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

    // MARK: - Derived UI state

    /// True when the freshest data is older than ~2 poll cycles, or an error stands.
    var isStale: Bool {
        if lastError != nil { return true }
        guard let updated = lastUpdated else { return true }
        return Date().timeIntervalSince(updated) > refreshSeconds * 2 + 5
    }

    var barState: BarState {
        if let err = lastError, status == nil {
            _ = err
            return .off
        }
        guard let s = status else { return .loading }
        if lastError != nil { return .off }
        return s.score.onTrack ? .onTrack : .drifting
    }

    /// The number shown in the menu bar, or "--" when we have nothing.
    var scoreText: String {
        guard let s = status, lastError == nil || status != nil else { return "--" }
        return "\(s.score.overall)"
    }
}
