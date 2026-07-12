import SwiftUI

/// The rich popover shown by `MenuBarExtra(.window)`.
///
/// Sections (per the brief):
///   1. Header — score, on-track text, gear placeholder
///   2. NOW    — current app -> goal, on/off-task dot
///   3. Goals  — name + pct + target, compact
///   4. Footer — screenpipe / backend / last-capture health + Refresh + Quit
struct PopoverView: View {
    @ObservedObject var store: StatusStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().padding(.vertical, 8)
            nowLine
            Divider().padding(.vertical, 8)
            goalsList
            Divider().padding(.vertical, 8)
            footer
        }
        .padding(14)
        .frame(width: 320)
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
            // Gear placeholder — settings wired up later.
            Menu {
                Text("Settings coming soon")
                Button("Refresh now") { store.refresh() }
            } label: {
                Image(systemName: "gearshape")
                    .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .frame(width: 22)
        }
    }

    private var onTrackText: String {
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

    // MARK: - 3. Goals list

    private var goalsList: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("GOALS")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
                Spacer()
                if !(store.status?.week.sparkline.isEmpty ?? true) {
                    Text(store.status!.week.sparkline)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
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
        }
    }

    // MARK: - 4. Footer

    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let err = store.lastError {
                Label(err, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .lineLimit(2)
            }
            HStack(spacing: 10) {
                healthChip(
                    ok: store.status?.health.screenpipe.ok ?? false,
                    label: "screenpipe"
                )
                healthChip(
                    ok: store.status?.health.backend.ollamaOk ?? false,
                    label: store.status?.health.backend.defaultBackend ?? "backend"
                )
                Spacer()
                Text(lastCaptureText)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            HStack {
                Button {
                    store.refresh()
                } label: {
                    Label(store.isRefreshing ? "Refreshing…" : "Refresh",
                          systemImage: "arrow.clockwise")
                }
                .disabled(store.isRefreshing)
                Spacer()
                Button(role: .destructive) {
                    NSApplication.shared.terminate(nil)
                } label: {
                    Label("Quit", systemImage: "power")
                }
            }
            .font(.callout)
        }
    }

    private func healthChip(ok: Bool, label: String) -> some View {
        HStack(spacing: 4) {
            Circle()
                .fill(ok ? Color.green : Color.secondary)
                .frame(width: 6, height: 6)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private var lastCaptureText: String {
        guard let cap = store.status?.health.lastCapture, !cap.isEmpty else {
            return "no capture"
        }
        // Show just the time portion when we can find it; otherwise the raw string.
        if let tIdx = cap.firstIndex(of: "T") {
            let after = cap[cap.index(after: tIdx)...]
            let time = after.prefix(5) // HH:MM
            return "cap \(time)"
        }
        return cap
    }
}

/// One compact goal row: colored dot + name, then `pct% / target%`.
struct GoalRowView: View {
    let goal: GoalRow

    var body: some View {
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
    }

    private func pct(_ v: Double) -> String {
        String(Int(v.rounded()))
    }
}
