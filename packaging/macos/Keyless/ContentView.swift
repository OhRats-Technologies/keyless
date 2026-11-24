// ContentView: native three-pane shell — a real NavigationSplitView sidebar, a
// calm Home dashboard, Activity, and grouped Settings. The engine supplies all
// truth; the shell never parses status prose or guesses calibration state.

import AppKit
import Combine
import SwiftUI

struct ContentView: View {
    enum AppTab: String, Hashable {
        case home, activity, settings
    }

    @ObservedObject var engine: EngineBridge
    @ObservedObject var audio: AudioLevelMonitor
    @Binding var showingOnboarding: Bool
    @State private var tab: AppTab = .home

    var body: some View {
        Group {
            if showingOnboarding {
                OnboardingView(engine: engine) { showingOnboarding = false }
            } else {
                NavigationSplitView {
                    SidebarView(
                        engine: engine,
                        selection: $tab,
                        openSetup: openSetup
                    )
                } detail: {
                    switch tab {
                    case .activity: ActivityView(engine: engine)
                    case .settings: SettingsView(
                        engine: engine,
                        openSetup: openSetup,
                        showOnboarding: { showingOnboarding = true }
                    )
                    case .home: HomeView(engine: engine, audio: audio, openSetup: openSetup)
                    }
                }
            }
        }
        .frame(minWidth: showingOnboarding ? 1000 : 900,
               minHeight: showingOnboarding ? 680 : 600)
        .onAppear { resizeWindow(immersive: showingOnboarding) }
        .onChange(of: showingOnboarding) { _, showing in resizeWindow(immersive: showing) }
    }

    /// The setup is a full-window surface, not a second window. Resize in place
    /// so the single-instance lock and TCC responsibility never change.
    private func resizeWindow(immersive: Bool) {
        guard let window = NSApp.keyWindow ?? NSApp.windows.first else { return }
        let size = immersive ? NSSize(width: 1060, height: 720) : NSSize(width: 980, height: 640)
        var frame = window.frame
        frame.size = size
        window.setFrame(frame, display: true, animate: true)
        window.center()
    }

    private func openSetup() {
        showingOnboarding = true
    }
}

// MARK: - Sidebar

private struct SidebarView: View {
    @ObservedObject var engine: EngineBridge
    @Binding var selection: ContentView.AppTab
    let openSetup: () -> Void
    @Environment(\.colorScheme) private var scheme

    private var theme: Theme { Theme(scheme) }

    var body: some View {
        List(selection: $selection) {
            Section {
                Label("Home", systemImage: "house").tag(ContentView.AppTab.home)
                Label("Activity", systemImage: "clock.arrow.circlepath").tag(ContentView.AppTab.activity)
                Label("Settings", systemImage: "gearshape").tag(ContentView.AppTab.settings)
            } header: {
                HStack(spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: Tokens.radius_Default, style: .continuous)
                            .fill(LinearGradient(colors: [theme.accent, theme.accent.opacity(0.7)],
                                                 startPoint: .topLeading,
                                                 endPoint: .bottomTrailing))
                        Image(systemName: "eye.fill")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(theme.accentText)
                    }
                    .frame(width: 26, height: 26)
                    VStack(alignment: .leading, spacing: 0) {
                        Text("Keyless").font(.headline)
                        Text(engine.state == "listening" ? "Listening" : "Idle")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 4)
            }

            if !engine.setupComplete {
                Section("Setup") {
                    Button { openSetup() } label: {
                        Label("Finish setup", systemImage: "sparkles")
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("Keyless")
        .safeAreaInset(edge: .bottom) {
            HStack(spacing: 8) {
                Circle()
                    .fill(engine.state == "listening" ? theme.success
                          : engine.detail.contains("Not connected") ? theme.warning : theme.muted)
                    .frame(width: 7, height: 7)
                Text(engine.detail.contains("Not connected") ? "Offline" : "Connected")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
    }
}

// MARK: - Home

struct HomeView: View {
    @ObservedObject var engine: EngineBridge
    @ObservedObject var audio: AudioLevelMonitor
    @Environment(\.colorScheme) private var scheme
    let openSetup: () -> Void

    private var theme: Theme { Theme(scheme) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                hero
                // The engine contract document drives Readiness, Devices and
                // Recent; the hero stays shell chrome and covers Status/Action.
                if let home = engine.ui?.home {
                    SectionListView(
                        sections: home.sections.filter {
                            $0.title != "Status" && $0.title != "Action"
                        },
                        onAction: { row in
                            Task { await engine.uiAction(contractActionPayload(for: row)) }
                        }
                    )
                }
                microphone
            }
            .padding(28)
            .frame(maxWidth: 880, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(theme.background)
        .navigationTitle("Home")
        .onAppear { audio.startMonitoring() }
        .onDisappear { audio.stopMonitoring() }
    }

    private var needsSetup: Bool {
        !engine.setupComplete
    }

    private var hero: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(LinearGradient(
                    colors: [theme.accent.opacity(0.20), theme.accent.opacity(0.08)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                ))
            HStack(spacing: 24) {
                VStack(alignment: .leading, spacing: 10) {
                    Label("KEYLESS", systemImage: "eye.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(theme.accent)
                    Text(statusTitle)
                        .font(.system(size: 32, weight: .bold, design: .rounded))
                    Text(statusDetail)
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 10) {
                        Button(primaryTitle) { primaryAction() }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.large)
                    }
                    .padding(.top, 4)
                }
                Spacer(minLength: 12)
                gazeOrb
                    .frame(width: 116, height: 116)
            }
            .padding(26)
        }
        .frame(height: 200)
    }

    private var gazeOrb: some View {
        ZStack {
            ForEach([112.0, 84.0, 56.0], id: \.self) { size in
                Circle()
                    .stroke(theme.accent.opacity(size == 112 ? 0.18 : 0.30), lineWidth: 1)
                    .frame(width: size, height: size)
            }
            Circle()
                .fill(RadialGradient(colors: [theme.accent.opacity(0.5), theme.accent.opacity(0.05)],
                                     center: .center, startRadius: 2, endRadius: 30))
                .frame(width: 60, height: 60)
            Image(systemName: "eye.fill")
                .font(.system(size: 26, weight: .thin))
                .foregroundStyle(.primary)
        }
    }

    private var microphone: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 12) {
                    LiveWaveform(levels: audio.levels, isMuted: audio.isMuted)
                        .frame(maxWidth: .infinity, minHeight: 36, maxHeight: 36)

                    Button { audio.toggleMute() } label: {
                        Image(systemName: audio.isMuted ? "mic.slash.fill" : "mic.fill")
                            .frame(width: 16)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .tint(audio.isMuted ? theme.warning : theme.accent)
                    .help(audio.isMuted ? "Unmute microphone" : "Mute microphone")
                    .accessibilityLabel(audio.isMuted ? "Unmute microphone" : "Mute microphone")
                }

                Text(transcriptText)
                    .font(.caption)
                    .foregroundStyle(audio.transcript.isEmpty ? .secondary : .primary)
                    .lineLimit(1)
                    .truncationMode(.head)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.vertical, 8)
            .padding(.horizontal, 4)
        } label: {
            Label("Live audio", systemImage: "waveform")
                .font(.headline)
        }
    }

    private var transcriptText: String {
        if audio.isMuted { return "Microphone muted" }
        if !audio.transcriptionAvailable { return "Live transcription unavailable" }
        if audio.transcript.isEmpty { return "Listening for speech…" }
        return audio.transcript
    }

    private var statusTitle: String {
        if engine.detail.contains("Not connected") { return "Engine offline" }
        if engine.state == "listening" { return "Listening." }
        if !engine.setupComplete { return "Finish setup" }
        return "Ready when you are."
    }

    private var statusDetail: String {
        if engine.detail.contains("Not connected") {
            return "Start the engine with `keyless serve` to connect this app."
        }
        if engine.state == "listening" {
            return "Keyless is active. Look at something and say “click”."
        }
        if !engine.setupComplete {
            return "A short setup connects your devices and permissions."
        }
        return "Start listening whenever you like. Pause here or from the menu bar."
    }

    private var primaryTitle: String {
        if needsSetup { return "Open setup" }
        return engine.state == "listening" ? "Pause" : "Start listening"
    }

    private func primaryAction() {
        if needsSetup {
            openSetup()
        } else {
            // The engine's action registry owns start/pause; the shell only
            // posts the contract action for the current state.
            let action = engine.state == "listening" ? "pause" : "start"
            Task { await engine.uiAction(["action": action]) }
        }
    }
}

// MARK: - Activity

struct ActivityView: View {
    @ObservedObject var engine: EngineBridge

    var body: some View {
        Group {
            if let activity = engine.ui?.activity,
               activity.sections.contains(where: { !$0.rows.isEmpty }) {
                // Rows come from the engine contract; the shell adds layout only.
                List {
                    SectionListView(
                        sections: activity.sections,
                        onAction: { row in
                            Task { await engine.uiAction(contractActionPayload(for: row)) }
                        }
                    )
                }
            } else {
                ContentUnavailableView(
                    "No activity yet",
                    systemImage: "clock.arrow.circlepath",
                    description: Text("Actions you take with Keyless will appear here.")
                )
            }
        }
        .navigationTitle("Activity")
    }
}

// MARK: - Settings

struct SettingsView: View {
    @ObservedObject var engine: EngineBridge
    let openSetup: () -> Void
    let showOnboarding: () -> Void
    @State private var confirmingReset = false
    @Environment(\.colorScheme) private var scheme

    private var theme: Theme { Theme(scheme) }

    var body: some View {
        Form {
            // All configurable rows come from the engine contract document;
            // the shell contributes only macOS-specific chrome (TCC adapter
            // and diagnostics).
            if let settings = engine.ui?.settings {
                SectionListView(
                    sections: settings.sections,
                    onAction: { row in
                        Task { await engine.uiAction(contractActionPayload(for: row)) }
                    }
                )
            }

            Section {
                PermissionsView()
                Button("Review in System Settings") { openPrivacy() }
                Button("Reset all permissions…", role: .destructive) {
                    confirmingReset = true
                }
                Button("Run setup…") { showOnboarding() }
            } header: {
                Text("Permissions (macOS)")
            }

            Section {
                Button("Copy doctor report") { copyDoctor() }
            } header: {
                Text("Diagnostics")
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
        .confirmationDialog("Reset all permissions?", isPresented: $confirmingReset) {
            Button("Reset and restart", role: .destructive) {
                PermissionGuideController.shared.revokeAllPermissions()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Keyless will lose Camera, Microphone, Accessibility, and Screen Recording access. You'll be asked again after the restart.")
        }
    }

    private func openPrivacy() {
        NSWorkspace.shared.open(URL(string:
            "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension")!)
    }

    private func copyDoctor() {
        let text = (try? JSONSerialization.data(
            withJSONObject: engine.doctor, options: [.prettyPrinted]
        )).flatMap { String(data: $0, encoding: .utf8) } ?? "\(engine.doctor)"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

// MARK: - Menu bar

struct MenuBarView: View {
    @ObservedObject var engine: EngineBridge
    @Binding var showingOnboarding: Bool

    var body: some View {
        Text(engine.state == "listening" ? "Keyless — Listening" : "Keyless")
        Button(engine.state == "listening" ? "Pause" : "Start listening") {
            // The engine's action registry owns start/pause; the menu bar only
            // posts the contract action for the current state.
            let action = engine.state == "listening" ? "pause" : "start"
            Task { await engine.uiAction(["action": action]) }
        }
        Divider()
        Button("Finish setup…") { showingOnboarding = true }
        Button("Open Keyless…") { NSApp.activate(ignoringOtherApps: true) }
        Button("Quit") { NSApp.terminate(nil) }
    }
}
