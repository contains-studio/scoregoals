import SwiftUI
import AppKit

@main
struct ScoreGoalsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = StatusStore()

    var body: some Scene {
        MenuBarExtra {
            PopoverView(store: store)
                .onAppear { store.start() }
        } label: {
            // The label view is rendered into the status item at launch, so its
            // onAppear is the reliable place to kick off polling — it fires even
            // when the popover is never opened. start() is idempotent.
            MenuBarLabel(store: store)
                .onAppear { store.start() }
        }
        .menuBarExtraStyle(.window)

        // A real Settings window, opened from the popover's gear menu via
        // openWindow(id: "settings"). It shares the single StatusStore so writes
        // and the live poll cadence stay in sync with the menu bar.
        Window("ScoreGoals Settings", id: ScoreGoalsWindow.settings) {
            SettingsView(store: store)
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)
    }
}

/// Window identifiers used with `openWindow(id:)`.
enum ScoreGoalsWindow {
    static let settings = "settings"
}

/// Forces accessory activation (no Dock icon, no menu bar app menu) as a belt-and-
/// braces companion to LSUIElement=1 in Info.plist. Also kicks off the first poll
/// even if the popover is never opened.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }
}

/// The menu bar item itself: a gauge glyph + the numeric score, tinted by state
/// and dimmed when the data is stale / the engine is erroring.
struct MenuBarLabel: View {
    @ObservedObject var store: StatusStore

    private var symbol: String {
        switch store.barState {
        case .onTrack:  return "gauge.with.dots.needle.67percent"
        case .drifting: return "gauge.with.dots.needle.33percent"
        case .off:      return "exclamationmark.triangle.fill"
        case .loading:  return "gauge.with.dots.needle.0percent"
        }
    }

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: symbol)
            Text(store.scoreText)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .monospacedDigit()
        }
        .foregroundStyle(store.barState.tint)
        .opacity(store.isStale ? 0.45 : 1.0)
    }
}
