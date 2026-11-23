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

    private func openSetup(_ step: SetupStep?) {
        if let step {
            Task { await engine.setupAction("go_to", step: step.rawValue) }
        }
        showingOnboarding = true
    }
}

// MARK: - Sidebar

private struct SidebarView: View {
    @ObservedObject var engine: EngineBridge
    @Binding var selection: ContentView.AppTab
    let openSetup: (SetupStep?) -> Void

    var body: some View {
        List(selection: $selection) {
            Section {
                Label("Home", systemImage: "house").tag(ContentView.AppTab.home)
                Label("Activity", systemImage: "clock.arrow.circlepath").tag(ContentView.AppTab.activity)
                Label("Settings", systemImage: "gearshape").tag(ContentView.AppTab.settings)
            } header: {
                HStack(spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(LinearGradient(colors: [.blue, .purple],
                                                 startPoint: .topLeading,
                                                 endPoint: .bottomTrailing))
                        Image(systemName: "eye.fill")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(.white)
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

            if !engine.setupComplete || !engine.calibrationReady {
                Section("Setup") {
                    Button { openSetup(nil) } label: {
                        Label(engine.setupComplete ? "Calibrate" : "Finish setup",
                              systemImage: "sparkles")
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
                    .fill(engine.state == "listening" ? .green
                          : engine.detail.contains("Not connected") ? .orange : .secondary)
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
    let openSetup: (SetupStep?) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                hero
                microphone
            }
            .padding(28)
            .frame(maxWidth: 880, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .navigationTitle("Home")
        .onAppear { audio.startMonitoring() }
        .onDisappear { audio.stopMonitoring() }
    }

    private var needsSetup: Bool {
        !engine.setupComplete || !engine.calibrationReady
    }

    private var hero: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(LinearGradient(
                    colors: [Color.accentColor.opacity(0.20), Color.purple.opacity(0.12)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                ))
            HStack(spacing: 24) {
                VStack(alignment: .leading, spacing: 10) {
                    Label("KEYLESS", systemImage: "eye.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.accentColor)
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
                    .stroke(Color.accentColor.opacity(size == 112 ? 0.18 : 0.30), lineWidth: 1)
                    .frame(width: size, height: size)
            }
            Circle()
                .fill(RadialGradient(colors: [Color.accentColor.opacity(0.5), Color.accentColor.opacity(0.05)],
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
                    .tint(audio.isMuted ? .orange : Color.accentColor)
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
        if !engine.calibrationReady { return "Calibrate to begin" }
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
            return "A short setup connects your devices and aligns the eye cursor."
        }
        if !engine.calibrationReady {
            return "Eye tracking has no saved calibration yet; align the eye cursor from Settings."
        }
        return "Start listening whenever you like. Pause here or from the menu bar."
    }

    private var primaryTitle: String {
        if needsSetup { return "Open setup" }
        return engine.state == "listening" ? "Pause" : "Start listening"
    }

    private func primaryAction() {
        if needsSetup {
            openSetup(engine.calibrationReady ? nil : .calibration)
        } else if engine.state == "listening" {
            Task { await engine.pause() }
        } else {
            Task { await engine.start() }
        }
    }
}

// MARK: - Activity

struct ActivityView: View {
    @ObservedObject var engine: EngineBridge

    var body: some View {
        Group {
            if engine.recent.isEmpty {
                ContentUnavailableView(
                    "No activity yet",
                    systemImage: "clock.arrow.circlepath",
                    description: Text("Actions you take with Keyless will appear here.")
                )
            } else {
                List(engine.recent) { item in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.transcript).font(.body.weight(.medium))
                        HStack(spacing: 8) {
                            Text(item.route)
                            Text("·")
                            Text(item.target.isEmpty ? "No target" : item.target)
                            Text("·")
                            Text(item.result)
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .navigationTitle("Activity")
    }
}

// MARK: - Settings

struct SettingsView: View {
    @ObservedObject var engine: EngineBridge
    let openSetup: (SetupStep?) -> Void
    let showOnboarding: () -> Void
    @State private var confirmingReset = false
    @State private var applyingBackend = false

    var body: some View {
        Form {
            Section {
                LabeledContent {
                    HStack(spacing: 7) {
                        Circle().fill(stateColor).frame(width: 8, height: 8)
                        Text(engine.state.capitalized)
                    }
                } label: {
                    Label("Status", systemImage: "bolt.fill")
                }
                LabeledContent("Connection") {
                    Text(engine.baseURL.absoluteString).foregroundStyle(.secondary)
                }
            } header: {
                Text("Engine")
            }

            Section {
                Toggle("Always listen for voice commands", isOn: boolBinding("always_on_voice"))
                Toggle("Only act on command phrases", isOn: boolBinding("require_command_prefix"))
                Toggle("Pop-click (lip pop triggers a click)", isOn: boolBinding("enable_pop_click"))
                LabeledContent("Push-to-talk key") {
                    let key = stringBinding("ptt_hotkey").wrappedValue
                    Text(key.isEmpty ? "—" : key).foregroundStyle(.secondary)
                }
            } header: {
                Text("Voice")
            }

            Section {
                LabeledContent("Speech model") {
                    Text(((engine.doctor["stt"] as? [String: Any])?["selected"] as? String) ?? "Unknown")
                        .foregroundStyle(.secondary)
                }
                if cuaDriverReady {
                    Picker("Click & key driver", selection: actionBackendBinding) {
                        Text("Built-in").tag("native")
                        Text("cua-driver").tag("cua")
                    }
                    .disabled(applyingBackend)
                } else {
                    LabeledContent("Click & key driver") {
                        Text("Built-in").foregroundStyle(.secondary)
                    }
                }
                LabeledContent("Driver runtime") {
                    HStack(spacing: 6) {
                        if applyingBackend {
                            ProgressView().controlSize(.mini)
                            Text("Restarting…").foregroundStyle(.secondary)
                        } else {
                            Circle().fill(cuaDriverReady ? .green : .orange)
                                .frame(width: 7, height: 7)
                            Text(cuaDriverReady ? "Ready · \(cuaDriverVersion)" : "Not downloaded")
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            } header: {
                Text("Speech & actions")
            } footer: {
                Text(cuaDriverReady
                     ? "cua-driver draws the eye cursor in its own click-through overlay and delivers clicks, typing and hotkeys through the app's accessibility tree. Your mouse pointer is never warped. Changing this restarts listening."
                     : "To switch drivers, install the pinned runtime: keyless models download cua-driver")
            }

            Section {
                Picker("Camera setup", selection: stringBinding("camera_mode")) {
                    Text("Automatic").tag("auto")
                    Text("One camera").tag("mono")
                    Text("Two cameras").tag("stereo")
                }
                Toggle("Show eye cursor (doesn't move your mouse)", isOn: eyeCursorBinding)
                Text("Look at your mouse and say “steer to mouse” whenever the eye cursor drifts. No dot calibration.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle("Learn from my mouse clicks", isOn: clickLearningBinding)
                Text("Deliberate resting clicks teach the gaze model where you were looking ~180 ms before the click. Quick drag-clicks are ignored.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle("Adapt to posture and drift", isOn: boolBinding("gaze_online_ekf"))
                LabeledContent("Calibration") {
                    HStack(spacing: 6) {
                        Circle().fill(engine.calibrationReady ? .green : .orange)
                            .frame(width: 7, height: 7)
                        Text(engine.calibrationReady ? "Saved" : "No saved calibration")
                            .foregroundStyle(.secondary)
                    }
                }
                Button("Align eye cursor…") { openSetup(.calibration) }
                Button("Run setup…") { showOnboarding() }
            } header: {
                Text("Camera & gaze")
            }

            Section {
                PermissionsView()
                Button("Review in System Settings") { openPrivacy() }
                Button("Reset all permissions…", role: .destructive) {
                    confirmingReset = true
                }
            } header: {
                Text("Permissions")
            }

            Section {
                Picker("Backend", selection: stringBinding("llm_provider")) {
                    Text("Automatic").tag("auto")
                    Text("Ollama (local)").tag("ollama")
                    Text("API provider").tag("openai_compat")
                    Text("ACP agent").tag("acp")
                }
                TextField("Model", text: stringBinding("llm_model"))
                TextField("ACP agent", text: stringBinding("acp_agent_id"))
                Toggle("Allow screen crops to remote AI", isOn: boolBinding("gaze_vlm_remote_consent"))
            } header: {
                Text("AI assistant (optional)")
            }

            Section {
                ForEach(capabilityRows) { row in
                    LabeledContent(row.name) {
                        Text(row.status).foregroundStyle(.secondary)
                    }
                }
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

    private func boolBinding(_ key: String) -> Binding<Bool> {
        Binding(
            get: { engine.config[key] as? Bool ?? false },
            set: { value in Task { await engine.saveConfig([key: value]) } }
        )
    }

    private var cuaDriver: [String: Any] {
        engine.doctor["cua_driver"] as? [String: Any] ?? [:]
    }

    private var cuaDriverReady: Bool { cuaDriver["ready"] as? Bool ?? false }

    private var cuaDriverVersion: String { cuaDriver["version"] as? String ?? "—" }

    /// The daemon builds its pointer backend at spawn time, so a config write
    /// alone leaves the running session on the old driver.
    private var actionBackendBinding: Binding<String> {
        Binding(
            get: { engine.config["action_backend"] as? String ?? "native" },
            set: { value in
                guard value != (engine.config["action_backend"] as? String ?? "native") else { return }
                Task {
                    applyingBackend = true
                    let wasListening = engine.state == "listening"
                    await engine.saveConfig(["action_backend": value])
                    if wasListening {
                        await engine.pause()
                        await engine.start()
                    }
                    applyingBackend = false
                }
            }
        )
    }

    /// The eye overlay is created when the engine daemon spawns, so toggling
    /// it while listening needs the same restart as a driver change.
    private var eyeCursorBinding: Binding<Bool> {
        Binding(
            get: { engine.config["gaze_cursor_control"] as? Bool ?? false },
            set: { value in
                Task {
                    let wasListening = engine.state == "listening"
                    await engine.saveConfig(["gaze_cursor_control": value])
                    if wasListening {
                        await engine.pause()
                        await engine.start()
                    }
                }
            }
        )
    }

    private var clickLearningBinding: Binding<Bool> {
        Binding(
            get: { engine.config["gaze_click_learning"] as? Bool ?? true },
            set: { value in
                Task {
                    let wasListening = engine.state == "listening"
                    await engine.saveConfig(["gaze_click_learning": value])
                    if wasListening {
                        await engine.pause()
                        await engine.start()
                    }
                }
            }
        )
    }

    private func stringBinding(_ key: String) -> Binding<String> {
        Binding(
            get: { engine.config[key] as? String ?? "" },
            set: { value in Task { await engine.saveConfig([key: value]) } }
        )
    }

    private var stateColor: Color {
        switch engine.state {
        case "listening": return .green
        case "error": return .red
        case "paused": return .orange
        default: return .secondary
        }
    }

    private struct CapabilityRow: Identifiable {
        let id: String
        let name: String
        let status: String
    }

    private var capabilityRows: [CapabilityRow] {
        guard let caps = (engine.doctor["capabilities"] as? [String: Any])?["capabilities"] as? [String: Any]
        else { return [] }
        return [
            ("Pointer", "pointer"), ("Cameras", "cameras"),
            ("Screen", "screen_capture"), ("Overlay", "overlay"),
        ].compactMap { label, key in
            guard let info = caps[key] as? [String: Any],
                  let status = info["status"] as? String else { return nil }
            return CapabilityRow(id: key, name: label, status: status.capitalized)
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
            Task {
                if engine.state == "listening" { await engine.pause() }
                else { await engine.start() }
            }
        }
        Divider()
        Button("Calibrate…") {
            Task { await engine.setupAction("go_to", step: "calibration") }
            showingOnboarding = true
        }
        Button("Open Keyless…") { NSApp.activate(ignoringOtherApps: true) }
        Button("Quit") { NSApp.terminate(nil) }
    }
}
