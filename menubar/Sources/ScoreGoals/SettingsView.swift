import SwiftUI
import AppKit
import ServiceManagement

/// Launch-at-login backed by `SMAppService.mainApp`. Registration can fail when
/// the app is ad-hoc signed / run from outside /Applications — callers surface
/// the thrown error as a hint rather than crashing.
enum LoginItem {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    static func setEnabled(_ on: Bool) throws {
        if on {
            if SMAppService.mainApp.status != .enabled {
                try SMAppService.mainApp.register()
            }
        } else {
            // .notRegistered / .notFound need no unregister call.
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            }
        }
    }
}

/// Real Settings window opened from the gear menu. Reads current values from
/// `config --json`; every control writes back through the engine (or, for the
/// engine path + login item, through UserDefaults / SMAppService).
struct SettingsView: View {
    @ObservedObject var store: StatusStore

    // Local mirrors of engine/system state. Loaded (not typed) values are set
    // directly on these vars; user edits go through side-effecting Bindings so a
    // programmatic load never triggers a write back to the engine.
    @State private var backend = "ollama"
    @State private var nudges = true
    @State private var capturePaused = false
    @State private var refreshChoice = 30
    @State private var enginePath = ""
    @State private var loginEnabled = false
    @State private var loginHint: String?
    @State private var geminiKey = ""

    // Goals editor state. `goalsText` is the editable buffer; `goalsOriginal` is
    // the last loaded/saved content (the "edited" dot compares the two).
    @State private var goalsText = ""
    @State private var goalsOriginal = ""
    @State private var goalsInitialized = false
    @State private var goalsMessage: ActionMessage?

    private let refreshOptions: [(label: String, seconds: Int)] =
        [("15s", 15), ("30s", 30), ("1m", 60), ("5m", 300)]

    var body: some View {
        Form {
            Section("Engine") {
                Picker("Default backend", selection: backendBinding) {
                    Text("ollama").tag("ollama")
                    Text("gemini").tag("gemini")
                    Text("both").tag("both")
                }
                Picker("Refresh cadence", selection: refreshBinding) {
                    ForEach(refreshOptions, id: \.seconds) { opt in
                        Text(opt.label).tag(opt.seconds)
                    }
                }
            }

            Section("Behavior") {
                Toggle("Nudges enabled", isOn: nudgesBinding)
                Toggle("Pause capture", isOn: captureBinding)
            }

            Section("Sensors") {
                HStack(spacing: 8) {
                    Circle()
                        .fill(screenpipeOK ? Color.green : Color.secondary)
                        .frame(width: 8, height: 8)
                    Text("screenpipe")
                    Spacer()
                    if screenpipeOK {
                        Text("running").font(.caption).foregroundStyle(.secondary)
                    } else {
                        Link("Download screenpipe",
                             destination: URL(string: "https://screenpi.pe")!)
                            .font(.caption)
                    }
                }
                Text(screenpipeOK
                     ? "Local capture is active."
                     : "screenpipe is an external app. Install it from screenpi.pe and grant Screen Recording + Microphone; scoregoals detects it at localhost:3030.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            Section("Learning") {
                HStack {
                    Text("Learned rules")
                    Spacer()
                    Text("\(store.status?.learning.activeRules ?? 0)")
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                HStack {
                    Text("Corrections / wk trend")
                    Spacer()
                    Text(correctionsTrend)
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Text(learningBlurb)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            Section("Gemini API key (BYOK)") {
                SecureField("Paste key to enable Gemini (optional)", text: $geminiKey)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    Button("Save key") {
                        store.setGeminiKey(geminiKey)
                        geminiKey = ""
                    }
                    .disabled(geminiKey.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button("Clear") {
                        store.setGeminiKey("")
                        geminiKey = ""
                    }
                    Spacer()
                    Text(store.geminiKeyIsSet ? "key: set" : "key: not set")
                        .font(.caption)
                        .foregroundStyle(store.geminiKeyIsSet ? .green : .secondary)
                }
                Text("Stored locally in data/settings.json (gitignored), never shown back. Without a key, analysis stays local (ollama) with the gemini CLI OAuth fallback if installed.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            Section("Goals") {
                Text("Edit goals.md — one \"## Goal: <name>\" block per goal, each with a keywords line and an optional target_pct.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                TextEditor(text: $goalsText)
                    .font(.system(.body, design: .monospaced))
                    .frame(height: 300)
                    .overlay(
                        RoundedRectangle(cornerRadius: 5)
                            .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
                    )
                    .disabled(!goalsInitialized)
                HStack(spacing: 8) {
                    Button {
                        goalsMessage = nil
                        store.saveGoals(goalsText) { ok, line in
                            if ok { goalsOriginal = goalsText }
                            goalsMessage = ActionMessage(text: line, isError: !ok)
                        }
                    } label: {
                        if store.busyActions.contains("goals") {
                            ProgressView().controlSize(.small)
                        } else {
                            Text("Save goals")
                        }
                    }
                    .disabled(store.busyActions.contains("goals")
                              || !goalsInitialized
                              || goalsText == goalsOriginal)
                    if goalsInitialized, goalsText != goalsOriginal {
                        Circle().fill(Color.orange).frame(width: 7, height: 7)
                        Text("edited").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Reload") { store.loadGoals() }
                    Button("Open file") { store.openGoalsFile() }
                }
                if let msg = goalsMessage {
                    Label(msg.text,
                          systemImage: msg.isError ? "exclamationmark.triangle.fill" : "checkmark.circle")
                        .font(.caption)
                        .foregroundStyle(msg.isError ? .red : .green)
                        .lineLimit(2)
                }

                if !store.goalsList.isEmpty {
                    Divider()
                    Text("Retire goals without editing the file — archived goals stay in goals.md but drop out of alignment, targets, and drift.")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    ForEach(store.goalsList) { goal in
                        GoalArchiveRow(goal: goal, store: store)
                    }
                }
            }

            Section("Engine location") {
                TextField("Repo directory or engine binary", text: $enginePath)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    Button("Apply path") { store.setEnginePath(enginePath) }
                    Spacer()
                }
                Text(store.engineInvocation)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(2)
                Text("Leave empty to use the default repo. Overrides via $SCOREGOALS_BIN (or legacy $DAYLOOP_BIN) still win.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            Section("Startup") {
                Toggle("Launch at login", isOn: loginBinding)
                if let hint = loginHint {
                    Text(hint).font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 400, height: 720)
        .onAppear(perform: load)
        .onChange(of: store.config) { _, cfg in
            if let cfg { apply(cfg) }
        }
        .onChange(of: store.goalsRaw) { _, raw in
            applyGoals(raw)
        }
    }

    // MARK: - Bindings (read local @State, write through the engine on edit)

    private var backendBinding: Binding<String> {
        Binding(get: { backend }, set: { backend = $0; store.setConfig("default_backend", $0) })
    }
    private var refreshBinding: Binding<Int> {
        Binding(get: { refreshChoice }, set: { refreshChoice = $0; store.setRefreshSeconds($0) })
    }
    private var nudgesBinding: Binding<Bool> {
        Binding(get: { nudges }, set: { nudges = $0; store.setConfig("nudges_enabled", $0 ? "true" : "false") })
    }
    private var captureBinding: Binding<Bool> {
        Binding(get: { capturePaused }, set: { capturePaused = $0; store.setConfig("capture_paused", $0 ? "true" : "false") })
    }
    private var loginBinding: Binding<Bool> {
        Binding(get: { loginEnabled }, set: { setLogin($0) })
    }

    // MARK: - Load / apply

    private var screenpipeOK: Bool {
        store.status?.health.screenpipe.ok ?? false
    }

    /// "12 → 3" from the learning KPI's weekly correction counts (first→last),
    /// a single count when there's one week, or "—" when there's no history yet.
    private var correctionsTrend: String {
        let counts = (store.status?.learning.correctionsByWeek ?? []).map(\.count)
        switch counts.count {
        case 0:  return "—"
        case 1:  return "\(counts[0])"
        default: return "\(counts.first!) → \(counts.last!)"
        }
    }

    private var learningBlurb: String {
        let thisWeek = store.status?.correctionsThisWeek ?? 0
        return "\(thisWeek) correction\(thisWeek == 1 ? "" : "s") this week. "
            + "Confirming the engine's guesses promotes learned rules, so corrections trend toward zero."
    }

    private func load() {
        store.loadConfig()
        store.loadGeminiKeyState()
        store.loadGoals()
        enginePath = UserDefaults.standard.string(forKey: ScoreGoalsDefaults.enginePathKey) ?? ""
        loginEnabled = LoginItem.isEnabled
        if let cfg = store.config { apply(cfg) }
        if store.goalsLoaded { applyGoals(store.goalsRaw) }
    }

    /// Load fetched goals.md text into the editor, but only the first time (or
    /// after a Reload resets it) — never clobber edits the user has in flight.
    private func applyGoals(_ raw: String) {
        guard !goalsInitialized || goalsText == goalsOriginal else { return }
        goalsText = raw
        goalsOriginal = raw
        goalsInitialized = true
    }

    private func apply(_ cfg: ScoreGoalsConfig) {
        backend = cfg.defaultBackend
        nudges = cfg.nudgesEnabled
        capturePaused = cfg.capturePaused
        refreshChoice = nearestRefresh(cfg.refreshSeconds)
    }

    private func nearestRefresh(_ seconds: Int) -> Int {
        refreshOptions.min(by: { abs($0.seconds - seconds) < abs($1.seconds - seconds) })?.seconds ?? 30
    }

    private func setLogin(_ on: Bool) {
        do {
            try LoginItem.setEnabled(on)
            loginEnabled = LoginItem.isEnabled
            loginHint = on ? "ScoreGoals will open at login." : nil
        } catch {
            // Reflect the real status; explain why it didn't take.
            loginEnabled = LoginItem.isEnabled
            loginHint = "Couldn't \(on ? "enable" : "disable") login item: "
                + "\(error.localizedDescription). Move ScoreGoals.app to /Applications and try again."
        }
    }
}

/// One compact goal row in the Settings Goals list: name + target, an
/// "archived" tag when retired, and an Archive/Unarchive button that edits
/// goals.md in place via the engine.
struct GoalArchiveRow: View {
    let goal: GoalSummary
    @ObservedObject var store: StatusStore

    private var busy: Bool { store.busyActions.contains("goals") }

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(goal.name)
                        .font(.callout)
                        .foregroundStyle(goal.archived ? .secondary : .primary)
                        .strikethrough(goal.archived, color: .secondary)
                        .lineLimit(1)
                    if goal.archived {
                        Text("archived")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Color.secondary.opacity(0.15), in: Capsule())
                    }
                }
                if let target = goal.targetPct {
                    Text("target \(Int(target.rounded()))%")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer()
            Button(goal.archived ? "Unarchive" : "Archive") {
                store.setGoalArchived(goal.goalId, archived: !goal.archived)
            }
            .font(.caption)
            .disabled(busy)
        }
    }
}
