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
    @State private var showHistory = false
    /// Header score tapped -> per-goal evidence breakdown (source: review --json).
    @State private var showEvidence = false
    /// Review pane "N more…" expander (collapsed shows the 6 biggest).
    @State private var reviewExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if showEvidence, scoreIsEvidenceable {
                evidenceView
            }
            Divider().padding(.vertical, 8)
            nowLine
            Divider().padding(.vertical, 8)
            intentionsSection
            Divider().padding(.vertical, 8)
            reviewSection
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
            Button {
                withAnimation(.easeInOut(duration: 0.18)) { showEvidence.toggle() }
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(scoreNumber)
                            .font(.system(size: 30, weight: .bold, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(store.barState.tint)
                        if scoreShowsDenominator {
                            Text("/ 100")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        if scoreIsEvidenceable {
                            Image(systemName: showEvidence ? "chevron.up" : "chevron.down")
                                .font(.system(size: 9, weight: .semibold))
                                .foregroundStyle(.tertiary)
                                .padding(.leading, 1)
                        }
                    }
                    Text(onTrackText)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    if let date = store.status?.date, !date.isEmpty {
                        Text(date)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .buttonStyle(.plain)
            .disabled(!scoreIsEvidenceable)
            .help(scoreIsEvidenceable
                  ? (showEvidence ? "Hide score breakdown" : "Show score breakdown")
                  : "")
            Spacer()
            Menu {
                Button("Settings…") { openSettings() }
                Button("Refresh now") { store.refresh(); store.loadReview() }
                Divider()
                Button("Quit ScoreGoals") { NSApplication.shared.terminate(nil) }
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .frame(width: 22)
        }
    }

    /// The big header number: "--" before first poll, "—" on an unscored day,
    /// else the score. Never force-unwraps a nullable `overall`.
    private var scoreNumber: String {
        guard let s = store.status else { return "--" }
        guard s.score.scored, let overall = s.score.overall else { return "—" }
        return "\(overall)"
    }

    /// "/ 100" only reads right next to a real number.
    private var scoreShowsDenominator: Bool {
        guard let s = store.status else { return false }
        return s.score.scored && s.score.overall != nil
    }

    /// The breakdown only makes sense once we have a scored day with sessions.
    private var scoreIsEvidenceable: Bool {
        scoreShowsDenominator
    }

    private func openSettings() {
        NSApp.activate(ignoringOtherApps: true)
        openWindow(id: ScoreGoalsWindow.settings)
    }

    private var onTrackText: String {
        if !store.engineResolved { return "engine not found — set path in Settings" }
        if store.status == nil, store.lastError != nil { return "engine unavailable" }
        guard let s = store.status else { return "loading…" }
        if !s.score.scored {
            return "not enough captured time yet (\(Int(s.score.activeMinutes))m)"
        }
        switch store.barState {
        case .onTrack:  return "on track"
        case .drifting: return "drifting"
        default:        return s.score.onTrack ? "on track" : "drifting"
        }
    }

    // MARK: - Score evidence breakdown (review --json, grouped by assignment)

    /// The score breakdown shown under the header when the score is tapped.
    /// Groups every reviewed session by its resolved assignment, biggest first.
    private var evidenceView: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                sectionHeader("SCORE BREAKDOWN")
                Spacer()
                if let am = store.review?.score.activeMinutes {
                    Text("\(Int(am))m active")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
            }
            if let groups = evidenceGroups {
                if groups.isEmpty {
                    Text("no sessions captured yet")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(groups) { group in
                        EvidenceGroupView(group: group)
                    }
                }
            } else if let err = store.reviewError {
                Label(err, systemImage: "exclamationmark.triangle")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            } else {
                Text("loading…")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.top, 8)
    }

    /// Sessions grouped by assignment label, each with a minutes total, ordered
    /// biggest-first. Nil while review has never loaded (drives the loading state).
    private var evidenceGroups: [EvidenceGroup]? {
        guard let sessions = store.review?.sessions else { return nil }
        let grouped = Dictionary(grouping: sessions, by: { $0.assignmentLabel })
        return grouped.map { label, sess in
            EvidenceGroup(id: label,
                          label: label,
                          minutes: sess.reduce(0) { $0 + $1.minutes },
                          sessions: sess.sorted { $0.minutes > $1.minutes })
        }
        .sorted { $0.minutes > $1.minutes }
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
            HStack {
                sectionHeader("TODAY'S THREE")
                Spacer()
                if let rate = store.status?.intentions.historySummary?.completionRate {
                    Text("\(Int((rate * 100).rounded()))% / 7d")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                        .help("7-day intention completion rate")
                }
            }
            let items = store.status?.intentions.items ?? []
            if items.isEmpty {
                IntentionsEditor(store: store)
            } else {
                let maxAttr = max(items.map(\.attributedMinutes).max() ?? 0, 1)
                ForEach(items) { item in
                    IntentionRow(item: item, maxMinutes: maxAttr, store: store)
                }
            }
            historyDisclosure
        }
    }

    /// Compact "History" disclosure: the last 7 days as `date — n/n done` rows,
    /// loaded from `today history --json` the first time it's opened.
    private var historyDisclosure: some View {
        DisclosureGroup(isExpanded: $showHistory) {
            if let hist = store.history {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(hist.daysList) { day in
                        HStack(spacing: 6) {
                            Text(shortDay(day.date))
                                .font(.caption2.monospacedDigit())
                                .foregroundStyle(.secondary)
                                .frame(width: 52, alignment: .leading)
                            if day.items.contains(where: { $0.carriedFrom != nil }) {
                                Image(systemName: "arrow.uturn.backward")
                                    .font(.system(size: 8))
                                    .foregroundStyle(.tertiary)
                                    .help("has carried-over items")
                            }
                            Spacer()
                            Text(day.nTotal == 0 ? "—" : "\(day.nDone)/\(day.nTotal)")
                                .font(.caption2.monospacedDigit())
                                .foregroundStyle(day.nTotal > 0 && day.nDone == day.nTotal ? .green : .secondary)
                        }
                    }
                }
                .padding(.top, 3)
            } else {
                Text("loading…").font(.caption2).foregroundStyle(.tertiary)
            }
        } label: {
            Text("History")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .onChange(of: showHistory) { _, open in
            if open { store.loadHistory() }
        }
    }

    /// "2026-07-11" -> "Jul 11" for the compact history rows.
    private func shortDay(_ iso: String) -> String {
        guard let d = StatusStore.parseISO(iso + "T00:00:00") else {
            return String(iso.suffix(5))  // MM-DD fallback
        }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "MMM d"
        return f.string(from: d)
    }

    // MARK: - 3b. REVIEW (needs_review sessions -> one-gesture corrections)

    /// Sessions still awaiting a correction, biggest minutes first (the engine
    /// already returns uncertain-first; we re-sort to be certain).
    private var reviewPending: [ReviewSession] {
        (store.review?.sessions ?? [])
            .filter { $0.needsReview }
            .sorted { $0.minutes > $1.minutes }
    }

    /// Active goals for the per-row goal picker (drop the `unaligned` pseudo-goal).
    private var reviewGoals: [GoalRow] { focusGoals }

    /// Pending sessions that already have an assignment `--confirm` can accept.
    private var confirmAllCount: Int { reviewPending.filter { $0.verdict != nil }.count }

    private var reviewSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                sectionHeader("REVIEW")
                if !reviewPending.isEmpty {
                    Text("\(reviewPending.count)")
                        .font(.caption2.weight(.semibold).monospacedDigit())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Color.orange, in: Capsule())
                }
                Spacer()
            }
            reviewBody
        }
    }

    @ViewBuilder private var reviewBody: some View {
        if store.review == nil {
            if let err = store.reviewError {
                Label(err, systemImage: "exclamationmark.triangle")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            } else {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("loading review…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } else if reviewPending.isEmpty {
            Label("All reviewed", systemImage: "checkmark.seal.fill")
                .font(.callout)
                .foregroundStyle(.green)
        } else {
            let visible = reviewExpanded ? reviewPending : Array(reviewPending.prefix(6))
            ForEach(visible) { session in
                ReviewRow(session: session, goals: reviewGoals, store: store)
            }
            HStack(spacing: 8) {
                if reviewPending.count > 6 {
                    Button(reviewExpanded ? "Show less"
                                          : "\(reviewPending.count - 6) more…") {
                        withAnimation { reviewExpanded.toggle() }
                    }
                    .font(.caption)
                    .buttonStyle(.borderless)
                }
                Spacer()
                if confirmAllCount > 0 {
                    Button {
                        store.confirmAllReview()
                    } label: {
                        if store.busyActions.contains("confirm-all") {
                            ProgressView().controlSize(.small)
                        } else {
                            Text("Confirm all (\(confirmAllCount))")
                        }
                    }
                    .font(.caption)
                    .buttonStyle(.bordered)
                    .disabled(store.busyActions.contains("confirm-all"))
                }
            }
            .padding(.top, 2)
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
                actionButton("Refresh", "arrow.clockwise", key: "refresh") { store.refresh(); store.loadReview() }
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
                HStack(spacing: 4) {
                    if let carried = item.carriedFrom {
                        Image(systemName: "arrow.uturn.backward")
                            .font(.system(size: 9))
                            .foregroundStyle(.orange)
                            .help("carried over from \(carried)")
                    }
                    Text(item.text)
                        .font(.callout)
                        .strikethrough(item.done, color: .secondary)
                        .foregroundStyle(item.done ? .secondary : .primary)
                        .lineLimit(2)
                }
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

// MARK: - Review

/// One review row: minutes + app + title on top, one-gesture correction controls
/// (goal picker · Off-track · Not work · ✓ confirm) below — or a score-delta flash
/// after a label lands. Every control calls `label` through the same CLI surface.
struct ReviewRow: View {
    let session: ReviewSession
    let goals: [GoalRow]
    @ObservedObject var store: StatusStore

    private var busy: Bool { store.busyActions.contains("label-\(session.id)") }
    private var flash: String? { store.reviewFlash[session.id] }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            // Line 1 — minutes · app · title (title truncates in the middle).
            HStack(spacing: 6) {
                Text("\(Int(session.minutes.rounded()))m")
                    .font(.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(minWidth: 28, alignment: .leading)
                Text(session.app ?? "unknown")
                    .font(.caption.weight(.medium))
                    .lineLimit(1)
                if let title = session.title, !title.isEmpty {
                    Text(title)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer(minLength: 4)
            }

            // Line 2 — the flash, else the correction controls.
            if let flash {
                Label(flash, systemImage: "checkmark.circle.fill")
                    .font(.caption.monospacedDigit().weight(.medium))
                    .foregroundStyle(.green)
            } else {
                HStack(spacing: 8) {
                    Menu {
                        ForEach(goals) { g in
                            Button(g.goalName) {
                                store.labelSession(session.id, ["--goal", g.goalId])
                            }
                        }
                    } label: {
                        HStack(spacing: 3) {
                            Image(systemName: "tag")
                                .font(.system(size: 9))
                            Text(currentGoalLabel)
                                .lineLimit(1)
                            Image(systemName: "chevron.down")
                                .font(.system(size: 8, weight: .semibold))
                        }
                        .frame(maxWidth: 130, alignment: .leading)
                    }
                    .menuStyle(.borderlessButton)
                    .font(.caption2)
                    .disabled(busy || goals.isEmpty)

                    Spacer(minLength: 4)

                    if busy {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Off-track") {
                            store.labelSession(session.id, ["--off-track"])
                        }
                        .font(.caption2)
                        .buttonStyle(.borderless)
                        .help("worked, but on no goal")

                        Button("Not work") {
                            store.labelSession(session.id, ["--not-work"])
                        }
                        .font(.caption2)
                        .buttonStyle(.borderless)
                        .help("out of scope — excluded from active time")

                        Button {
                            store.labelSession(session.id, ["--confirm"])
                        } label: {
                            Image(systemName: "checkmark")
                                .font(.system(size: 11, weight: .semibold))
                        }
                        .buttonStyle(.borderless)
                        .foregroundStyle(session.verdict == nil ? Color.secondary : Color.green)
                        .disabled(session.verdict == nil)
                        .help(session.verdict == nil
                              ? "no current assignment to confirm"
                              : "confirm \(session.assignmentLabel)")
                    }
                }
            }
        }
        .padding(.vertical, 3)
    }

    /// The goal picker's current selection: the resolved goal, or a prompt.
    private var currentGoalLabel: String {
        if let name = session.goalName, !name.isEmpty { return name }
        switch session.verdict {
        case "off_track": return "Off-track"
        case "not_work":  return "Not work"
        default:          return "Pick goal"
        }
    }
}

// MARK: - Score evidence

/// One assignment group in the score breakdown: a goal (or Off-track / Not work /
/// Unmatched) with its total minutes and the read-only sessions behind it.
struct EvidenceGroup: Identifiable {
    let id: String
    let label: String
    let minutes: Double
    let sessions: [ReviewSession]
}

struct EvidenceGroupView: View {
    let group: EvidenceGroup

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Circle()
                    .fill(dotColor)
                    .frame(width: 6, height: 6)
                Text(group.label)
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
                Spacer(minLength: 6)
                Text("\(Int(group.minutes.rounded()))m")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ForEach(group.sessions) { s in
                HStack(spacing: 6) {
                    Text(s.span)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                        .frame(width: 86, alignment: .leading)
                    Text(s.app ?? "—")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    Text("\(Int(s.minutes.rounded()))m")
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                .padding(.leading, 12)
            }
        }
    }

    private var dotColor: Color {
        switch group.label {
        case "Off-track", "Unmatched": return .orange
        case "Not work":               return .secondary
        default:                        return .green
        }
    }
}
