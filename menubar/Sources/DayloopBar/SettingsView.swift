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
                     : "screenpipe is an external app. Install it from screenpi.pe and grant Screen Recording + Microphone; dayloop detects it at localhost:3030.")
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

            Section("Engine location") {
                TextField("Repo directory or engine binary", text: $enginePath)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    Button("Apply path") { store.setEnginePath(enginePath) }
                    Button("Edit goals.md") { store.openGoalsFile() }
                    Spacer()
                }
                Text(store.engineInvocation)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(2)
                Text("Leave empty to use the default repo. Overrides via $DAYLOOP_BIN still win.")
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
        .frame(width: 400, height: 620)
        .onAppear(perform: load)
        .onChange(of: store.config) { _, cfg in
            if let cfg { apply(cfg) }
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

    private func load() {
        store.loadConfig()
        store.loadGeminiKeyState()
        enginePath = UserDefaults.standard.string(forKey: DayloopDefaults.enginePathKey) ?? ""
        loginEnabled = LoginItem.isEnabled
        if let cfg = store.config { apply(cfg) }
    }

    private func apply(_ cfg: DayloopConfig) {
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
            loginHint = on ? "DayloopBar will open at login." : nil
        } catch {
            // Reflect the real status; explain why it didn't take.
            loginEnabled = LoginItem.isEnabled
            loginHint = "Couldn't \(on ? "enable" : "disable") login item: "
                + "\(error.localizedDescription). Move DayloopBar.app to /Applications and try again."
        }
    }
}
