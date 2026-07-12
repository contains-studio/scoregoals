import SwiftUI
import AppKit

/// The rich, interactive popover shown by `MenuBarExtra(.window)`.
///
/// Sections:
///   1. Header        — score, on-track text, gear menu (Settings / Refresh / Quit)
///   2. NOW           — current app -> goal, on/off-task dot
///   3. TODAY'S THREE — checkable intentions with time attribution, or an editor
///   4. FOCUS         — active block + Stop, or a goal menu + minutes stepper
///   5. GOALS         — per-goal progress vs target, weekly bars, streak, next event
///   6. QUICK ACTIONS — capture / EOD report / plan / refresh
///   7. Footer        — health chips, last error
struct PopoverView: View {
    @ObservedObject var store: StatusStore
    @Environment(\.openWindow) private var openWindow
    @State private var focusMinutes = 50

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().padding(.vertical, 8)
            nowLine
            Divider().padding(.vertical, 8)
            intentionsSection
            Divider().padding(.vertical, 8)
            focusSection
            Divider().padding(.vertical, 8)
            goalsSection
            Divider().padding(.vertical, 8)
            quickActions
            Divider().padding(.vertical, 8)
            footer
        }
        .padding(14)
        .frame(width: 340)
    }

    // MARK: - 1. Header

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(store.status.map { "\($0.score.overall)" } ?? "--")
                        .font(.system(size: 30, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(store.barState.tint)
                    Text("/ 100")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text(onTrackText)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if let date = store.status?.date, !date.isEmpty {
                    Text(date)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer()
            Menu {
                Button("Settings…") { openSettings() }
                Button("Refresh now") { store.refresh() }
                Divider()
                Button("Quit Dayloop") { NSApplication.shared.terminate(nil) }
            } label: {
                Image(systemName: "gearshape")
                    .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .frame(width: 22)
        }
    }

    private func openSettings() {
        NSApp.activate(ignoringOtherApps: true)
        openWindow(id: DayloopWindow.settings)
    }

    private var onTrackText: String {
        if !store.engineResolved { return "engine not found — set path in Settings" }
        if store.status == nil, store.lastError != nil { return "engine unavailable" }
        guard let s = store.status else { return "loading…" }
        switch store.barState {
        case .onTrack:  return "on track"
        case .drifting: return "drifting"
        default:        return s.score.onTrack ? "on track" : "drifting"
        }
    }

    // MARK: - 2. NOW line

    private var nowLine: some View {
        let now = store.status?.now
        return HStack(spacing: 8) {
            Circle()
                .fill(nowDotColor)
                .frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text("NOW")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.tertiary)
                    Text(nowPrimary)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                }
                if let sub = nowSecondary {
                    Text(sub)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            if let now, now.minutes > 0 {
                Text("\(Int(now.minutes))m")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var nowDotColor: Color {
        guard let now = store.status?.now else { return .secondary }
        switch now.source {
        case "screenpipe": return now.onTask ? .green : .orange
        default:           return .secondary   // idle / unknown
        }
    }

    private var nowPrimary: String {
        guard let now = store.status?.now else { return "—" }
        switch now.source {
        case "idle":    return "idle"
        case "unknown": return "no sensor"
        default:        return now.app ?? "unknown"
        }
    }

    private var nowSecondary: String? {
        guard let now = store.status?.now, now.source == "screenpipe" else {
            if store.status?.now.source == "unknown" {
                return "screenpipe not reachable"
            }
            return nil
        }
        if let goal = now.goalName {
            return "→ \(goal)"
        }
        return now.onTask ? nil : "off-task"
    }

    // MARK: - 3. TODAY'S THREE (intentions)

    private var intentionsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("TODAY'S THREE")
            let items = store.status?.intentions.items ?? []
            if items.isEmpty {
                IntentionsEditor(store: store)
            } else {
                let maxAttr = max(items.map(\.attributedMinutes).max() ?? 0, 1)
                ForEach(items) { item in
                    IntentionRow(item: item, maxMinutes: maxAttr, store: store)
                }
            }
        }
    }

    // MARK: - 4. FOCUS

    private var focusSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("FOCUS")
            if let focus = store.status?.focus, focus.active {
                HStack(spacing: 8) {
                    Image(systemName: "target").foregroundStyle(.green)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(focus.goalName ?? focus.goalId ?? "focus block")
                            .font(.callout.weight(.medium)).lineLimit(1)
                        Text(focusRemaining(focus))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { store.stopFocus() } label: {
                        if store.busyActions.contains("focus") {
                            ProgressView().controlSize(.small)
                        } else {
                            Text("Stop")
                        }
                    }
                    .disabled(store.busyActions.contains("focus"))
                }
            } else {
                HStack(spacing: 8) {
                    Menu {
                        ForEach(focusGoals) { goal in
                            Button(goal.goalName) {
                                store.startFocus(goalId: goal.goalId, minutes: focusMinutes)
                            }
                        }
                    } label: {
                        Label("Start focus block", systemImage: "target")
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    .disabled(focusGoals.isEmpty || store.busyActions.contains("focus"))

                    Spacer()
                    Stepper("\(focusMinutes)m", value: $focusMinutes, in: 10...120, step: 5)
                        .fixedSize()
                        .font(.caption)
                }
            }
        }
    }

    /// Real goals only (drop the trailing `unaligned` pseudo-goal).
    private var focusGoals: [GoalRow] {
        (store.status?.goals ?? []).filter { $0.goalId != "unaligned" && !$0.goalId.isEmpty }
    }

    private func focusRemaining(_ focus: Focus) -> String {
        guard let until = focus.until, let deadline = StatusStore.parseISO(until) else {
            return "active"
        }
        let mins = Int(deadline.timeIntervalSinceNow / 60)
        return mins > 0 ? "\(mins)m left" : "expiring…"
    }

    // MARK: - 5. GOALS

    private var goalsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                sectionHeader("TIME ON GOAL")
                Button { openSettings() } label: {
                    Image(systemName: "pencil").font(.caption2)
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.tertiary)
                .help("Edit goals.md")
                Spacer()
                if let week = store.status?.week, week.onTrackDays > 0 || !week.scores.isEmpty {
                    HStack(spacing: 6) {
                        WeekBars(scores: week.scores)
                        Text("\(week.onTrackDays)/7")
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
            if let goals = store.status?.goals, !goals.isEmpty {
                ForEach(goals) { goal in
                    GoalRowView(goal: goal)
                }
            } else {
                Text("no goal data yet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let event = store.status?.nextEvent {
                HStack(spacing: 6) {
                    Image(systemName: "calendar")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text(event.title)
                        .font(.caption)
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    Text(eventCountdown(event))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                .padding(.top, 2)
            }
        }
    }

    private func eventCountdown(_ event: NextEvent) -> String {
        let mins = Int(event.minutesUntil)
        if mins <= 0 { return "now" }
        if mins < 60 { return "in \(mins)m" }
        return "in \(mins / 60)h \(mins % 60)m"
    }

    // MARK: - 6. QUICK ACTIONS

    private var quickActions: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("QUICK ACTIONS")
            HStack(spacing: 8) {
                actionButton("Capture", "camera.viewfinder", key: "capture") { store.captureNow() }
                actionButton("EOD", "doc.text", key: "report") { store.generateReport() }
                actionButton("Plan", "sun.max", key: "plan") { store.planDay() }
                actionButton("Refresh", "arrow.clockwise", key: "refresh") { store.refresh() }
            }
            if let msg = store.actionMessage {
                Label(msg.text, systemImage: msg.isError ? "exclamationmark.triangle.fill" : "checkmark.circle")
                    .font(.caption2)
                    .foregroundStyle(msg.isError ? .red : .green)
                    .lineLimit(2)
            }
        }
    }

    private func actionButton(_ title: String, _ symbol: String, key: String,
                              _ run: @escaping () -> Void) -> some View {
        let busy = key == "refresh" ? store.isRefreshing : store.busyActions.contains(key)
        return Button(action: run) {
            VStack(spacing: 3) {
                if busy {
                    ProgressView().controlSize(.small)
                        .frame(height: 15)
                } else {
                    Image(systemName: symbol).font(.system(size: 14))
                }
                Text(title).font(.caption2)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
        }
        .buttonStyle(.bordered)
        .disabled(busy)
    }

    // MARK: - 7. Footer

    private var footer: some View {
        let screenpipe = store.status?.health.screenpipe
        let screenpipeOK = screenpipe?.ok ?? false
        return VStack(alignment: .leading, spacing: 8) {
            if !store.engineResolved {
                HStack(spacing: 6) {
                    Label("engine not found", systemImage: "bolt.slash.fill")
                        .font(.caption2)
                        .foregroundStyle(.red)
                    Spacer(minLength: 6)
                    Button("Set path…") { openSettings() }
                        .font(.caption2)
                        .buttonStyle(.borderless)
                }
            }
            if let err = store.lastError {
                Label(err, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .lineLimit(2)
            }
            // Screenpipe is an external dependency: when it's unreachable, offer
            // the official download instead of trying to launch it ourselves.
            if !screenpipeOK {
                HStack(spacing: 6) {
                    Image(systemName: "sensor.tag.radiowaves.forward")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text("screenpipe not running")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 6)
                    Link("Download", destination: URL(string: "https://screenpi.pe")!)
                        .font(.caption2)
                }
            }
            HStack(spacing: 10) {
                healthChip(
                    ok: screenpipeOK,
                    label: "screenpipe",
                    help: screenpipeOK
                        ? (screenpipe?.detail ?? "reachable")
                        : "not reachable — install the desktop app at https://screenpi.pe"
                )
                healthChip(
                    ok: store.status?.health.backend.ollamaOk ?? false,
                    label: store.status?.health.backend.defaultBackend ?? "backend",
                    help: store.status?.health.backend.gemini == "off"
                        ? "ollama backend"
                        : "gemini: \(store.status?.health.backend.gemini ?? "off")"
                )
                Spacer()
                Text(lastCaptureText)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private func healthChip(ok: Bool, label: String, help: String) -> some View {
        HStack(spacing: 4) {
            Circle()
                .fill(ok ? Color.green : Color.secondary)
                .frame(width: 6, height: 6)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .help(help)
    }

    private var lastCaptureText: String {
        guard let cap = store.status?.health.lastCapture, !cap.isEmpty else {
            return "no capture"
        }
        if let tIdx = cap.firstIndex(of: "T") {
            let after = cap[cap.index(after: tIdx)...]
            let time = after.prefix(5) // HH:MM
            return "cap \(time)"
        }
        return cap
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.tertiary)
    }
}

// MARK: - Intentions

/// One checkable intention: toggle done, text, a time bar, and its apps.
struct IntentionRow: View {
    let item: Item
    let maxMinutes: Double
    @ObservedObject var store: StatusStore

    private var busy: Bool { store.busyActions.contains("today") }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Button { store.toggleIntention(item.id) } label: {
                Image(systemName: item.done ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 15))
                    .foregroundStyle(item.done ? Color.green : Color.secondary)
            }
            .buttonStyle(.plain)
            .disabled(busy)

            VStack(alignment: .leading, spacing: 3) {
                Text(item.text)
                    .font(.callout)
                    .strikethrough(item.done, color: .secondary)
                    .foregroundStyle(item.done ? .secondary : .primary)
                    .lineLimit(2)
                if item.attributedMinutes > 0 {
                    MiniBar(fraction: item.attributedMinutes / maxMinutes,
                            tint: item.done ? .secondary : .accentColor)
                }
                if !item.apps.isEmpty {
                    Text(item.apps.joined(separator: ", "))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                } else if let goal = item.goalName {
                    Text(goal)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 6)
            if item.attributedMinutes > 0 {
                Text("\(Int(item.attributedMinutes))m")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// Inline editor shown when there are no intentions yet. Three fields, joined on
/// "|" for `today set`.
struct IntentionsEditor: View {
    @ObservedObject var store: StatusStore
    @State private var a = ""
    @State private var b = ""
    @State private var c = ""

    private var busy: Bool { store.busyActions.contains("today") }
    private var parts: [String] {
        [a, b, c]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Set your top 3 intentions for the day.")
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField("First intention", text: $a).textFieldStyle(.roundedBorder)
            TextField("Second (optional)", text: $b).textFieldStyle(.roundedBorder)
            TextField("Third (optional)", text: $c).textFieldStyle(.roundedBorder)
            HStack {
                Spacer()
                Button {
                    store.setIntentions(parts)
                    a = ""; b = ""; c = ""
                } label: {
                    if busy {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("Set today's 3")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(busy || parts.isEmpty)
            }
        }
    }
}

// MARK: - Goals

/// One goal row: dot + name, `pct% / target%`, and a progress bar vs target.
struct GoalRowView: View {
    let goal: GoalRow

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Circle()
                    .fill(goal.onTrack ? Color.green : Color.orange)
                    .frame(width: 6, height: 6)
                Text(goal.goalName)
                    .font(.callout)
                    .lineLimit(1)
                Spacer(minLength: 8)
                HStack(spacing: 2) {
                    Text("\(pct(goal.pctTime))%")
                        .font(.callout.monospacedDigit().weight(.medium))
                    if let target = goal.targetPct {
                        Text("/ \(pct(target))%")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            if let target = goal.targetPct, target > 0 {
                MiniBar(fraction: goal.pctTime / target,
                        tint: goal.onTrack ? .green : .orange)
            }
        }
    }

    private func pct(_ v: Double) -> String { String(Int(v.rounded())) }
}

// MARK: - Small reusable pieces

/// A thin horizontal progress bar; `fraction` is clamped to 0…1.
struct MiniBar: View {
    let fraction: Double
    var tint: Color = .accentColor

    var body: some View {
        GeometryReader { geo in
            let clamped = min(max(fraction.isFinite ? fraction : 0, 0), 1)
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18))
                Capsule().fill(tint).frame(width: geo.size.width * clamped)
            }
        }
        .frame(height: 4)
    }
}

/// Seven tiny vertical bars for the week's scores (nil days render faint).
struct WeekBars: View {
    let scores: [Int?]

    var body: some View {
        HStack(alignment: .bottom, spacing: 2) {
            ForEach(Array(scores.enumerated()), id: \.offset) { _, score in
                let value = Double(score ?? 0)
                Capsule()
                    .fill(barColor(score))
                    .frame(width: 3, height: max(2, CGFloat(value / 100.0) * 16))
            }
        }
        .frame(height: 16, alignment: .bottom)
    }

    private func barColor(_ score: Int?) -> Color {
        guard let score else { return Color.secondary.opacity(0.25) }
        return score >= 60 ? .green : .orange
    }
}
