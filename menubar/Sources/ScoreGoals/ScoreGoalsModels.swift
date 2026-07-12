import Foundation

// MARK: - Codable models mirroring `scoregoals status --json` (schema_version = 1)
//
// Design goals:
//  - Tolerant: every field is optional-with-default. A missing key, a null, or a
//    wrong type never throws — it falls back to a safe default. This keeps the app
//    alive across additive schema changes and partial/degraded engine output.
//  - The only way decoding fails is if the top-level payload is not a JSON object
//    at all; StatusStore turns that into a visible error state rather than a crash.
//
// Each `init(from:)` uses `try? container.decode(...)` so any per-field failure
// yields `nil` and we substitute the documented fallback.

private extension KeyedDecodingContainer {
    /// Decode a value, or return `fallback` on missing key / null / type mismatch.
    func tolerant<T: Decodable>(_ type: T.Type, _ key: Key, _ fallback: T) -> T {
        (try? decode(T.self, forKey: key)) ?? fallback
    }
    /// Decode an optional value; missing key / null / type mismatch all yield nil.
    func optional<T: Decodable>(_ type: T.Type, _ key: Key) -> T? {
        try? decode(T.self, forKey: key)
    }
}

// MARK: - Top level

struct ScoreGoalsStatus: Codable {
    var schemaVersion: Int = 1
    var date: String = ""
    var generatedAt: String = ""
    var now: Now = Now()
    var score: Score = Score()
    var goals: [GoalRow] = []
    var driftFlags: [String] = []
    var intentions: Intentions = Intentions()
    var focus: Focus = Focus()
    var nextEvent: NextEvent? = nil
    var week: Week = Week()
    var health: Health = Health()
    var warnings: [String] = []

    init() {}

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case date
        case generatedAt = "generated_at"
        case now, score, goals
        case driftFlags = "drift_flags"
        case intentions, focus
        case nextEvent = "next_event"
        case week, health, warnings
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = c.tolerant(Int.self, .schemaVersion, 1)
        date = c.tolerant(String.self, .date, "")
        generatedAt = c.tolerant(String.self, .generatedAt, "")
        now = c.tolerant(Now.self, .now, Now())
        score = c.tolerant(Score.self, .score, Score())
        goals = c.tolerant([GoalRow].self, .goals, [])
        driftFlags = c.tolerant([String].self, .driftFlags, [])
        intentions = c.tolerant(Intentions.self, .intentions, Intentions())
        focus = c.tolerant(Focus.self, .focus, Focus())
        nextEvent = c.optional(NextEvent.self, .nextEvent)
        week = c.tolerant(Week.self, .week, Week())
        health = c.tolerant(Health.self, .health, Health())
        warnings = c.tolerant([String].self, .warnings, [])
    }
}

// MARK: - now

struct Now: Codable {
    var app: String? = nil
    var title: String? = nil
    var goalId: String? = nil
    var goalName: String? = nil
    var onTask: Bool = false
    var category: String? = nil
    var since: String? = nil
    var minutes: Double = 0
    var source: String = "unknown"

    init() {}

    enum CodingKeys: String, CodingKey {
        case app, title
        case goalId = "goal_id"
        case goalName = "goal_name"
        case onTask = "on_task"
        case category, since, minutes, source
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        app = c.optional(String.self, .app)
        title = c.optional(String.self, .title)
        goalId = c.optional(String.self, .goalId)
        goalName = c.optional(String.self, .goalName)
        onTask = c.tolerant(Bool.self, .onTask, false)
        category = c.optional(String.self, .category)
        since = c.optional(String.self, .since)
        minutes = c.tolerant(Double.self, .minutes, 0)
        source = c.tolerant(String.self, .source, "unknown")
    }
}

// MARK: - score

struct Score: Codable {
    var overall: Int = 0
    var onTrack: Bool = false
    var activeMinutes: Double = 0

    init() {}

    enum CodingKeys: String, CodingKey {
        case overall
        case onTrack = "on_track"
        case activeMinutes = "active_minutes"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        overall = c.tolerant(Int.self, .overall, 0)
        onTrack = c.tolerant(Bool.self, .onTrack, false)
        activeMinutes = c.tolerant(Double.self, .activeMinutes, 0)
    }
}

// MARK: - goals[]

struct GoalRow: Codable, Identifiable {
    var goalId: String = ""
    var goalName: String = ""
    var minutes: Double = 0
    var pctTime: Double = 0
    var targetPct: Double? = nil
    var onTrack: Bool = true

    var id: String { goalId }

    init() {}

    enum CodingKeys: String, CodingKey {
        case goalId = "goal_id"
        case goalName = "goal_name"
        case minutes
        case pctTime = "pct_time"
        case targetPct = "target_pct"
        case onTrack = "on_track"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        goalId = c.tolerant(String.self, .goalId, "")
        goalName = c.tolerant(String.self, .goalName, "")
        minutes = c.tolerant(Double.self, .minutes, 0)
        pctTime = c.tolerant(Double.self, .pctTime, 0)
        targetPct = c.optional(Double.self, .targetPct)
        onTrack = c.tolerant(Bool.self, .onTrack, true)
    }
}

// MARK: - intentions (same shape as `today --json`)

struct Intentions: Codable {
    var date: String = ""
    var setAt: String? = nil
    var items: [Item] = []
    var historySummary: HistorySummary? = nil

    init() {}

    enum CodingKeys: String, CodingKey {
        case date
        case setAt = "set_at"
        case items
        case historySummary = "history_summary"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = c.tolerant(String.self, .date, "")
        setAt = c.optional(String.self, .setAt)
        items = c.tolerant([Item].self, .items, [])
        historySummary = c.optional(HistorySummary.self, .historySummary)
    }
}

/// The cheap 7-day completion-rate rollup embedded in `status.intentions`.
struct HistorySummary: Codable {
    var days: Int = 7
    var completionRate: Double = 0

    init() {}

    enum CodingKeys: String, CodingKey {
        case days
        case completionRate = "completion_rate"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        days = c.tolerant(Int.self, .days, 7)
        completionRate = c.tolerant(Double.self, .completionRate, 0)
    }
}

struct Item: Codable, Identifiable {
    var id: String = ""
    var text: String = ""
    var goalId: String? = nil
    var goalName: String? = nil
    var done: Bool = false
    var attributedMinutes: Double = 0
    var apps: [String] = []
    /// The date this item was carried over from (yesterday's undone work), or nil.
    var carriedFrom: String? = nil

    init() {}

    enum CodingKeys: String, CodingKey {
        case id, text
        case goalId = "goal_id"
        case goalName = "goal_name"
        case done
        case attributedMinutes = "attributed_minutes"
        case apps
        case carriedFrom = "carried_from"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.tolerant(String.self, .id, UUID().uuidString)
        text = c.tolerant(String.self, .text, "")
        goalId = c.optional(String.self, .goalId)
        goalName = c.optional(String.self, .goalName)
        done = c.tolerant(Bool.self, .done, false)
        attributedMinutes = c.tolerant(Double.self, .attributedMinutes, 0)
        apps = c.tolerant([String].self, .apps, [])
        carriedFrom = c.optional(String.self, .carriedFrom)
    }
}

// MARK: - focus (same shape as `focus --json`)

struct Focus: Codable {
    var active: Bool = false
    var goalId: String? = nil
    var goalName: String? = nil
    var startedAt: String? = nil
    var until: String? = nil

    init() {}

    enum CodingKeys: String, CodingKey {
        case active
        case goalId = "goal_id"
        case goalName = "goal_name"
        case startedAt = "started_at"
        case until
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        active = c.tolerant(Bool.self, .active, false)
        goalId = c.optional(String.self, .goalId)
        goalName = c.optional(String.self, .goalName)
        startedAt = c.optional(String.self, .startedAt)
        until = c.optional(String.self, .until)
    }
}

// MARK: - next_event (object | null)

struct NextEvent: Codable {
    var title: String = ""
    var start: String = ""
    var minutesUntil: Double = 0

    init() {}

    enum CodingKeys: String, CodingKey {
        case title, start
        case minutesUntil = "minutes_until"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = c.tolerant(String.self, .title, "")
        start = c.tolerant(String.self, .start, "")
        minutesUntil = c.tolerant(Double.self, .minutesUntil, 0)
    }
}

// MARK: - week

struct Week: Codable {
    var scores: [Int?] = []
    var onTrackDays: Int = 0
    var sparkline: String = ""

    init() {}

    enum CodingKeys: String, CodingKey {
        case scores
        case onTrackDays = "on_track_days"
        case sparkline
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        scores = c.tolerant([Int?].self, .scores, [])
        onTrackDays = c.tolerant(Int.self, .onTrackDays, 0)
        sparkline = c.tolerant(String.self, .sparkline, "")
    }
}

// MARK: - health

struct Health: Codable {
    var screenpipe: ServiceStatus = ServiceStatus()
    var backend: Backend = Backend()
    var lastCapture: String? = nil
    var geminiCostTodayUsd: Double = 0
    var dataDirMb: Double = 0
    var capturePaused: Bool = false
    var nudgesEnabled: Bool = true

    init() {}

    enum CodingKeys: String, CodingKey {
        case screenpipe, backend
        case lastCapture = "last_capture"
        case geminiCostTodayUsd = "gemini_cost_today_usd"
        case dataDirMb = "data_dir_mb"
        case capturePaused = "capture_paused"
        case nudgesEnabled = "nudges_enabled"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        screenpipe = c.tolerant(ServiceStatus.self, .screenpipe, ServiceStatus())
        backend = c.tolerant(Backend.self, .backend, Backend())
        lastCapture = c.optional(String.self, .lastCapture)
        geminiCostTodayUsd = c.tolerant(Double.self, .geminiCostTodayUsd, 0)
        dataDirMb = c.tolerant(Double.self, .dataDirMb, 0)
        capturePaused = c.tolerant(Bool.self, .capturePaused, false)
        nudgesEnabled = c.tolerant(Bool.self, .nudgesEnabled, true)
    }
}

struct ServiceStatus: Codable {
    var ok: Bool = false
    var detail: String = ""

    init() {}

    enum CodingKeys: String, CodingKey {
        case ok, detail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.tolerant(Bool.self, .ok, false)
        detail = c.tolerant(String.self, .detail, "")
    }
}

// MARK: - config (`config --json`)

/// The effective app-mutable settings, decoded from `scoregoals config --json`.
/// Equatable so the Settings view can react to fresh values arriving async.
struct ScoreGoalsConfig: Codable, Equatable {
    var defaultBackend: String = "ollama"
    var nudgesEnabled: Bool = true
    var capturePaused: Bool = false
    var refreshSeconds: Int = 30
    var ollamaUrl: String = ""
    var geminiModel: String = ""

    init() {}

    enum CodingKeys: String, CodingKey {
        case defaultBackend = "default_backend"
        case nudgesEnabled = "nudges_enabled"
        case capturePaused = "capture_paused"
        case refreshSeconds = "refresh_seconds"
        case ollamaUrl = "ollama_url"
        case geminiModel = "gemini_model"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        defaultBackend = c.tolerant(String.self, .defaultBackend, "ollama")
        nudgesEnabled = c.tolerant(Bool.self, .nudgesEnabled, true)
        capturePaused = c.tolerant(Bool.self, .capturePaused, false)
        refreshSeconds = c.tolerant(Int.self, .refreshSeconds, 30)
        ollamaUrl = c.tolerant(String.self, .ollamaUrl, "")
        geminiModel = c.tolerant(String.self, .geminiModel, "")
    }
}

// MARK: - goals file (`goals --json`)

/// The goals.md surface returned by `scoregoals goals --json`: the file path, its
/// verbatim text (the Goals editor loads `raw` into its TextEditor), and the
/// parsed `goals[]` (used by the compact per-goal Archive/Unarchive list).
struct GoalsFile: Codable {
    var path: String = ""
    var raw: String = ""
    var goals: [GoalSummary] = []

    init() {}

    enum CodingKeys: String, CodingKey {
        case path, raw, goals
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = c.tolerant(String.self, .path, "")
        raw = c.tolerant(String.self, .raw, "")
        goals = c.tolerant([GoalSummary].self, .goals, [])
    }
}

/// One parsed goal from `goals --json` (includes archived goals, flagged).
struct GoalSummary: Codable, Identifiable {
    var goalId: String = ""
    var name: String = ""
    var targetPct: Double? = nil
    var archived: Bool = false

    var id: String { goalId }

    init() {}

    enum CodingKeys: String, CodingKey {
        case goalId = "id"
        case name
        case targetPct = "target_pct"
        case archived
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        goalId = c.tolerant(String.self, .goalId, "")
        name = c.tolerant(String.self, .name, "")
        targetPct = c.optional(Double.self, .targetPct)
        archived = c.tolerant(Bool.self, .archived, false)
    }
}

// MARK: - intentions history (`today history --json`)

/// The history rollup returned by `scoregoals today history --json`: per-day rows
/// plus an overall completion rate. Drives the "History" disclosure.
struct IntentionsHistory: Codable {
    var days: Int = 7
    var itemsTotal: Int = 0
    var itemsDone: Int = 0
    var completionRate: Double = 0
    var daysList: [HistoryDay] = []

    init() {}

    enum CodingKeys: String, CodingKey {
        case days
        case itemsTotal = "items_total"
        case itemsDone = "items_done"
        case completionRate = "completion_rate"
        case daysList = "days_list"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        days = c.tolerant(Int.self, .days, 7)
        itemsTotal = c.tolerant(Int.self, .itemsTotal, 0)
        itemsDone = c.tolerant(Int.self, .itemsDone, 0)
        completionRate = c.tolerant(Double.self, .completionRate, 0)
        daysList = c.tolerant([HistoryDay].self, .daysList, [])
    }
}

struct HistoryDay: Codable, Identifiable {
    var date: String = ""
    var nDone: Int = 0
    var nTotal: Int = 0
    var items: [HistoryItem] = []

    var id: String { date }

    init() {}

    enum CodingKeys: String, CodingKey {
        case date
        case nDone = "n_done"
        case nTotal = "n_total"
        case items
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = c.tolerant(String.self, .date, "")
        nDone = c.tolerant(Int.self, .nDone, 0)
        nTotal = c.tolerant(Int.self, .nTotal, 0)
        items = c.tolerant([HistoryItem].self, .items, [])
    }
}

struct HistoryItem: Codable, Identifiable {
    var id: String = ""
    var text: String = ""
    var done: Bool = false
    var carriedFrom: String? = nil

    init() {}

    enum CodingKeys: String, CodingKey {
        case id, text, done
        case carriedFrom = "carried_from"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.tolerant(String.self, .id, UUID().uuidString)
        text = c.tolerant(String.self, .text, "")
        done = c.tolerant(Bool.self, .done, false)
        carriedFrom = c.optional(String.self, .carriedFrom)
    }
}

struct Backend: Codable {
    // `default` is a Swift keyword — mapped to `defaultBackend`.
    var defaultBackend: String = "ollama"
    var ollamaOk: Bool = false
    var ollamaLatencyS: Double? = nil
    var gemini: String = "off"

    init() {}

    enum CodingKeys: String, CodingKey {
        case defaultBackend = "default"
        case ollamaOk = "ollama_ok"
        case ollamaLatencyS = "ollama_latency_s"
        case gemini
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        defaultBackend = c.tolerant(String.self, .defaultBackend, "ollama")
        ollamaOk = c.tolerant(Bool.self, .ollamaOk, false)
        ollamaLatencyS = c.optional(Double.self, .ollamaLatencyS)
        gemini = c.tolerant(String.self, .gemini, "off")
    }
}
