// OnboardingView: immersive first-run setup rendered from the engine-owned
// setup document. The step order is the canonical Welcome → Devices →
// Calibration → Ready used by the Linux shell and `keyless.ui.onboarding.flow`.
//
// macOS owns only the OS interaction: TCC prompts, deep links, and relaunch.
// Eye alignment is voice-guided inside this window; nothing here opens a
// full-screen calibration overlay.

import SwiftUI

enum SetupStep: String, CaseIterable {
    case welcome, devices, calibration, ready

    var title: String {
        switch self {
        case .welcome: return "Welcome"
        case .devices: return "Devices"
        case .calibration: return "Eye cursor"
        case .ready: return "Ready"
        }
    }
}

enum KeylessSetup {
    static let required: [KeylessPermission] = [.camera, .microphone, .accessibility]
    static let optional: [KeylessPermission] = [.screenRecording]
}

@MainActor
final class OnboardingFlow: ObservableObject {
    private enum Store {
        static let queue = "keyless.onboarding.queue"
        static let index = "keyless.onboarding.index"
        static let settingUp = "keyless.onboarding.settingUp"
    }

    let engine: EngineBridge
    let required = KeylessSetup.required
    let optional = KeylessSetup.optional

    @Published var grants: [KeylessPermission: PermissionGrant] = [:]
    @Published private(set) var settingUp = false
    @Published private(set) var activeSetup: KeylessPermission?
    @Published private(set) var setupIndex = 0
    @Published private(set) var setupTotal = 0

    private var timer: Timer?
    private var advanceWork: DispatchWorkItem?
    private var advanceScheduled = false
    private var setupQueue: [KeylessPermission] = []

    init(engine: EngineBridge) {
        self.engine = engine
        let defaults = UserDefaults.standard
        let rawQueue = (defaults.array(forKey: Store.queue) as? [Int]) ?? []
        setupQueue = rawQueue.compactMap { KeylessPermission(rawValue: $0) }
        setupIndex = defaults.integer(forKey: Store.index)
        setupTotal = setupQueue.count
        settingUp = defaults.bool(forKey: Store.settingUp) && !setupQueue.isEmpty
        refresh()
        if settingUp, setupIndex < setupQueue.count {
            activeSetup = setupQueue[setupIndex]
        }
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
    }

    deinit {
        timer?.invalidate()
        advanceWork?.cancel()
    }

    // MARK: Engine-owned step state

    var step: SetupStep {
        guard let raw = engine.setup?.state.step else { return .welcome }
        return SetupStep(rawValue: raw) ?? .welcome
    }

    func go(to step: SetupStep) {
        Task { await engine.setupAction("go_to", step: step.rawValue) }
    }

    func back() {
        Task { await engine.setupAction("back") }
    }

    func next() {
        Task { await engine.setupAction("advance") }
    }

    func finish() {
        stop()
        let defaults = UserDefaults.standard
        for key in [Store.queue, Store.index, Store.settingUp] {
            defaults.removeObject(forKey: key)
        }
        defaults.set(true, forKey: "keyless.onboarding.complete")
        Task { await engine.setupAction("complete") }
    }

    func skip() {
        stop()
        let defaults = UserDefaults.standard
        for key in [Store.queue, Store.index, Store.settingUp] {
            defaults.removeObject(forKey: key)
        }
        defaults.set(true, forKey: "keyless.onboarding.complete")
        Task { await engine.setupAction("complete") }
    }

    // MARK: Live state

    var requiredReady: Bool {
        required.allSatisfy { grants[$0] == .granted }
    }

    var allReady: Bool {
        (required + optional).allSatisfy { grants[$0] == .granted }
    }

    var calibrationReady: Bool { engine.calibrationReady }

    func refresh() {
        let guide = PermissionGuideController.shared
        for kind in required + optional {
            grants[kind] = guide.currentGrant(kind)
        }
        guard settingUp, let current = activeSetup else { return }
        if grants[current] == .granted { scheduleAdvance(after: 0.7) }
    }

    func start() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        advanceWork?.cancel()
        settingUp = false
        activeSetup = nil
    }

    // MARK: Permission tour — strictly one pane at a time

    /// Parallel Settings panes steal focus from each other, so ask sequentially
    /// and skip grants the OS already reports.
    func setUpAll() {
        setupQueue = (required + optional).filter { grants[$0] != .granted }
        setupTotal = setupQueue.count
        setupIndex = 0
        settingUp = !setupQueue.isEmpty
        persistSetup()
        if settingUp { startCurrentStep() }
    }

    func stopSetup() {
        advanceWork?.cancel()
        advanceScheduled = false
        settingUp = false
        activeSetup = nil
        setupQueue = []
        persistSetup()
    }

    func skipCurrent() {
        advanceWork?.cancel()
        advanceScheduled = false
        advance()
    }

    private func persistSetup() {
        let defaults = UserDefaults.standard
        defaults.set(setupQueue.map(\.rawValue), forKey: Store.queue)
        defaults.set(setupIndex, forKey: Store.index)
        defaults.set(settingUp, forKey: Store.settingUp)
    }

    private func startCurrentStep() {
        guard setupIndex < setupQueue.count else { stopSetup(); return }
        let kind = setupQueue[setupIndex]
        activeSetup = kind
        persistSetup()
        PermissionGuideController.shared.allowInline(kind)
    }

    private func scheduleAdvance(after delay: Double) {
        guard !advanceScheduled else { return }
        advanceScheduled = true
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.advanceScheduled = false
            guard self.settingUp else { return }
            Task { @MainActor in self.advance() }
        }
        advanceWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func advance() {
        guard settingUp else { return }
        setupIndex += 1
        if setupIndex >= setupQueue.count { stopSetup() }
        else { startCurrentStep() }
    }
}

// MARK: - Immersive onboarding window

struct OnboardingView: View {
    @ObservedObject var engine: EngineBridge
    @StateObject private var flow: OnboardingFlow
    @State private var pending: Set<KeylessPermission> = []
    @State private var revealedChecks = 0
    @State private var toast: String?
    var onDone: () -> Void

    init(engine: EngineBridge, onDone: @escaping () -> Void) {
        self.engine = engine
        self._flow = StateObject(wrappedValue: OnboardingFlow(engine: engine))
        self.onDone = onDone
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            background
            VStack(spacing: 0) {
                header
                stepper
                ZStack {
                    switch flow.step {
                    case .welcome: welcomeStep
                    case .devices:
                        ScrollView { devicesStep }
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    case .calibration: calibrationStep
                    case .ready: readyStep
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                footer
            }
            if let toast { toastBanner(toast) }
        }
        .frame(minWidth: 1000, minHeight: 680)
        .foregroundStyle(.white)
        .animation(.easeInOut(duration: 0.28), value: toast)
    }

    // MARK: Surface

    private var background: some View {
        ZStack {
            LinearGradient(
                colors: [
                    Tokens.Dark.surfaceAlt,
                    Tokens.Dark.surface,
                    Tokens.Dark.background,
                ],
                startPoint: .topLeading, endPoint: .bottomTrailing
            )
            RadialGradient(
                colors: [Tokens.Dark.accent.opacity(0.18), Color.clear],
                center: .topTrailing, startRadius: 40, endRadius: 620
            )
            RadialGradient(
                colors: [Tokens.Dark.accent.opacity(0.12), Color.clear],
                center: .bottomLeading, startRadius: 40, endRadius: 560
            )
        }
        .ignoresSafeArea()
    }

    private var header: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(LinearGradient(
                        colors: [Tokens.Dark.accent, Tokens.Dark.accent.opacity(0.7)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    ))
                Image(systemName: "eye.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(Tokens.Dark.accentText)
            }
            .frame(width: 30, height: 30)
            Text("Keyless")
                .font(.system(size: 15, weight: .semibold, design: .rounded))
            Spacer()
            if engine.state == "listening" {
                Label("Listening", systemImage: "waveform")
                    .font(.caption)
                    .foregroundStyle(Tokens.Dark.success)
            }
        }
        .padding(.horizontal, 56)
        .padding(.top, 26)
    }

    private var currentStepIndex: Int {
        SetupStep.allCases.firstIndex(of: flow.step) ?? 0
    }

    private var stepper: some View {
        HStack(spacing: 0) {
            ForEach(stepItems) { item in
                HStack(spacing: 8) {
                    ZStack {
                        Circle()
                            .fill(item.index <= currentStepIndex
                                  ? Tokens.Dark.accent : Tokens.Dark.text.opacity(0.08))
                            .frame(width: 22, height: 22)
                        if item.index < currentStepIndex {
                            Image(systemName: "checkmark")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(.white)
                        } else {
                            Text("\(item.index + 1)")
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(item.step == flow.step ? .white : .white.opacity(0.45))
                        }
                    }
                    Text(item.step.title)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(item.step == flow.step ? .white : .white.opacity(0.42))
                }
                if item.step != SetupStep.allCases.last {
                    Rectangle()
                        .fill(item.index < currentStepIndex
                              ? Tokens.Dark.accent.opacity(0.6) : Tokens.Dark.text.opacity(0.12))
                        .frame(height: 1)
                        .padding(.horizontal, 12)
                        .layoutPriority(-1)
                }
            }
        }
        .padding(.horizontal, 56)
        .padding(.top, 24)
    }

    private var footer: some View {
        HStack(spacing: 12) {
            if flow.step != .welcome {
                Button("Back") { flow.back() }
                    .buttonStyle(.plain)
                    .foregroundStyle(.white.opacity(0.62))
            }
            Spacer()
            if flow.step == .devices && (!flow.allReady || !engine.modelReady) {
                Button("Skip for now") { flow.skip(); onDone() }
                    .buttonStyle(.plain)
                    .foregroundStyle(.white.opacity(0.5))
            }
            switch flow.step {
            case .welcome:
                Button("Get started") { flow.go(to: .devices) }
                    .buttonStyle(.borderedProminent).controlSize(.large)
            case .devices:
                Button("Continue") { flow.next() }
                    .buttonStyle(.borderedProminent).controlSize(.large)
                    .disabled(!flow.requiredReady || !engine.modelReady)
            case .calibration:
                Button("Continue") { flow.next() }
                    .buttonStyle(.borderedProminent).controlSize(.large)
            case .ready:
                Button("Start listening") {
                    flow.finish()
                    Task { await engine.start() }
                    onDone()
                }
                .buttonStyle(.borderedProminent).controlSize(.large)
            }
        }
        .padding(.horizontal, 56)
        .padding(.bottom, 30)
    }

    // MARK: Steps

    private var welcomeStep: some View {
        HStack(spacing: 40) {
            VStack(alignment: .leading, spacing: 0) {
                Text("Keyless, on your Mac")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Tokens.Dark.accent.opacity(0.9))
                Text("Let Keyless\nuse this Mac")
                    .font(.system(size: 50, weight: .bold, design: .rounded))
                    .lineSpacing(2)
                    .padding(.top, 10)
                Text("Look at anything on your screen and speak to click, scroll, or type. Everything runs privately on this device.")
                    .font(.title3)
                    .foregroundStyle(.white.opacity(0.62))
                    .frame(maxWidth: 460, alignment: .leading)
                    .padding(.top, 18)
                HStack(spacing: 10) {
                    featureChip("eye.fill", "Gaze pointer")
                    featureChip("mic.fill", "Voice commands")
                    featureChip("hand.point.up.left.fill", "Clicks & keys")
                }
                .padding(.top, 26)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            ZStack {
                ForEach([300.0, 226.0, 152.0], id: \.self) { size in
                    Circle()
                        .stroke(Tokens.Dark.accent.opacity(size == 300 ? 0.10 : 0.18), lineWidth: 1)
                        .frame(width: size, height: size)
                }
                Circle()
                    .fill(RadialGradient(colors: [Tokens.Dark.accent.opacity(0.35), Tokens.Dark.accent.opacity(0.04)],
                                         center: .center, startRadius: 4, endRadius: 90))
                    .frame(width: 180, height: 180)
                Image(systemName: "eye.fill")
                    .font(.system(size: 74, weight: .thin))
                    .foregroundStyle(.white.opacity(0.9))
            }
            .frame(maxWidth: .infinity)
        }
        .padding(.horizontal, 56)
    }

    private var devicesStep: some View {
        VStack(spacing: 0) {
            stepTitle("Connect your devices",
                      "Confirm your camera, microphone, and speech engine are ready.")
            permissionCards.padding(.top, 14)
            modelCard.padding(.top, 12)
            if flow.settingUp { setupProgress.padding(.top, 16) }
            if !flow.settingUp && !flow.allReady {
                Button { flow.setUpAll() } label: {
                    Label("Set up all", systemImage: "sparkles").frame(maxWidth: 280)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .padding(.top, 18)
            }
            componentChecks
                .padding(.top, 16)
        }
        .padding(.horizontal, 56)
        .task {
            await engine.refresh()
            // Hex-style: the offline model starts preparing in the background
            // while the user reviews permissions; progress is engine-owned.
            if let model = engine.model, !model.ready,
               model.job?.state != "downloading", model.job?.state != "verifying" {
                await engine.downloadModel()
            }
            for index in 0..<componentChecksList.count {
                try? await Task.sleep(nanoseconds: 320_000_000)
                withAnimation { revealedChecks = index + 1 }
            }
        }
    }

    private var calibrationStep: some View {
        VStack(spacing: 0) {
            stepTitle("Gaze engine v2",
                      "No calibration grid. Voice control is ready now; the target-level model is in development.")
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 12) {
                    ZStack {
                        Circle().fill(Tokens.Dark.success.opacity(0.18))
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(Tokens.Dark.success)
                    }
                    .frame(width: 36, height: 36)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Voice control ready")
                            .font(.headline)
                        Text("The new model learns from your own clicks and corrections. No dot grid, no full-screen overlay, no startup gate.")
                            .font(.subheadline)
                            .foregroundStyle(.white.opacity(0.55))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                Divider().opacity(0.16)

                Text("What changes")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.62))
                Text("The eye cursor will rank the targets on screen and act on the one you mean. Until that model ships, use voice commands as usual.")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.55))
                    .fixedSize(horizontal: false, vertical: true)
            }
            .cardStyle()
            .padding(.top, 24)
            .frame(maxWidth: 620)
        }
        .padding(.horizontal, 56)
    }

    private var readyStep: some View {
        VStack(spacing: 0) {
            Spacer()
            ZStack {
                Circle().fill(Tokens.Dark.success.opacity(0.14)).frame(width: 84, height: 84)
                Image(systemName: "checkmark")
                    .font(.system(size: 36, weight: .semibold))
                    .foregroundStyle(Tokens.Dark.success)
            }
            Text("You're all set")
                .font(.system(size: 40, weight: .bold, design: .rounded))
                .padding(.top, 22)
            Text("Look at any button, link, or field and say “click”, “scroll down”, or “type …”.")
                .font(.title3)
                .foregroundStyle(.white.opacity(0.62))
                .multilineTextAlignment(.center)
                .padding(.top, 8)
            VStack(alignment: .leading, spacing: 10) {
                tip("Move naturally", "Head movement and multiple screens are tracked automatically.")
                tip("Learns from you", "The new gaze model learns from your own clicks and corrections — no recalibration ritual.")
                tip("Stay in control", "Pause or quit from the menu bar icon.")
            }
            .cardStyle()
            .frame(maxWidth: 480)
            .padding(.top, 28)
            Spacer()
        }
        .padding(.horizontal, 56)
    }

    // MARK: Components

    private var stepItems: [StepItem] {
        SetupStep.allCases.enumerated().map { StepItem(index: $0.offset, step: $0.element) }
    }

    private func stepTitle(_ title: String, _ subtitle: String) -> some View {
        VStack(spacing: 8) {
            Text(title)
                .font(.system(size: 34, weight: .bold, design: .rounded))
            Text(subtitle)
                .font(.body)
                .foregroundStyle(.white.opacity(0.6))
                .multilineTextAlignment(.center)
        }
        .padding(.top, 10)
    }

    private func tip(_ title: String, _ detail: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(Tokens.Dark.success)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.white.opacity(0.55))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var setupProgress: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(setupStatus).font(.subheadline).foregroundStyle(.white.opacity(0.8))
            }
            if let restartHint {
                Text(restartHint).font(.caption).foregroundStyle(Tokens.Dark.warning)
            }
            HStack(spacing: 18) {
                Button("Next") { flow.skipCurrent() }
                if restartHint != nil {
                    Button("Restart Keyless") { PermissionGuideController.shared.relaunch() }
                }
                Button("Stop") { flow.stopSetup() }
            }
            .buttonStyle(.plain)
            .font(.subheadline)
            .foregroundStyle(Tokens.Dark.accent)
        }
    }

    private var permissionCards: some View {
        let rows = flow.required + flow.optional
        return VStack(spacing: 0) {
            ForEach(rows, id: \.self) { kind in
                PermissionCard(
                    kind: kind,
                    grant: flow.grants[kind] ?? .unknown,
                    optional: !flow.required.contains(kind),
                    waiting: pending.contains(kind) || (flow.settingUp && flow.activeSetup == kind),
                    onAllow: { request(kind) }
                )
                if kind != rows.last { Divider().padding(.leading, 76) }
            }
        }
        .cardStyle()
        .frame(maxWidth: 620)
    }

    private var modelCard: some View {
        let model = engine.model
        let job = model?.job
        let busy = job?.state == "downloading" || job?.state == "verifying"
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Tokens.Dark.accent.opacity(0.18))
                        .frame(width: 42, height: 42)
                    Image(systemName: "waveform.and.mic")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(Tokens.Dark.accent)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Offline speech model").font(.headline)
                    Text("\(model?.name ?? "Parakeet Unified EN") · \(megabytes(model?.size ?? 0)) MB")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.55))
                }
                Spacer()
                if engine.modelReady {
                    Label("Ready", systemImage: "checkmark.circle.fill")
                        .font(.subheadline)
                        .foregroundStyle(Tokens.Dark.success)
                }
            }
            if busy, let job {
                VStack(alignment: .leading, spacing: 6) {
                    ProgressView(value: min(max(job.percent / 100, 0), 1))
                    HStack {
                        Text(job.state == "verifying"
                             ? "Checking…"
                             : "Downloading… \(Int(job.percent))% · \(megabytes(job.bytesDone)) / \(megabytes(job.bytesTotal)) MB")
                            .font(.caption)
                            .foregroundStyle(.white.opacity(0.65))
                        Spacer()
                        Button("Stop") { Task { await engine.cancelModelDownload() } }
                            .buttonStyle(.plain)
                            .foregroundStyle(Tokens.Dark.accent)
                            .font(.caption)
                    }
                }
            } else if let job, job.state == "failed" || job.state == "cancelled" {
                HStack {
                    Text(job.error.isEmpty ? "The download stopped." : job.error)
                        .font(.caption)
                        .foregroundStyle(Tokens.Dark.warning)
                    Spacer()
                    Button("Try again") { Task { await engine.downloadModel() } }
                        .buttonStyle(.plain)
                        .foregroundStyle(Tokens.Dark.accent)
                        .font(.caption)
                }
            } else if model?.ready == false {
                Button {
                    Task { await engine.downloadModel() }
                } label: {
                    Label("Download (\(megabytes(model?.size ?? 0)) MB)", systemImage: "arrow.down.circle")
                        .frame(maxWidth: 240)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
            Text("Runs on this device — your voice never leaves it.")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.4))
        }
        .cardStyle()
        .frame(maxWidth: 620, alignment: .leading)
    }

    private func megabytes(_ bytes: Int) -> Int {
        Int((Double(bytes) / 1_048_576).rounded())
    }

    private var componentChecksList: [SetupCheck] {
        let doctor = engine.doctor
        let cuaReady = (doctor["cua_driver"] as? [String: Any])?["ready"] as? Bool ?? false
        let agents = installedAgents
        return [
            SetupCheck(id: "engine", title: "Engine connected", done: !engine.detail.contains("Not connected")),
            SetupCheck(id: "cua", title: "Click & key driver ready", done: cuaReady),
            SetupCheck(id: "agents", title: agents.isEmpty ? "No agents yet" : "Agents: \(agents.joined(separator: ", "))", done: !agents.isEmpty),
        ]
    }

    private var componentChecks: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(componentChecksList.prefix(revealedChecks))) { check in
                HStack(spacing: 8) {
                    Image(systemName: check.done ? "checkmark.circle.fill" : "circle.dashed")
                        .foregroundStyle(check.done ? Tokens.Dark.success : Tokens.Dark.text.opacity(0.35))
                    Text(check.title).font(.caption)
                        .foregroundStyle(check.done ? .white.opacity(0.85) : .white.opacity(0.5))
                }
            }
        }
        .frame(maxWidth: 620, alignment: .leading)
    }

    private var installedAgents: [String] {
        ["claude", "codex", "opencode", "hermes"].filter { agentInstalled($0) }
    }

    private func agentInstalled(_ name: String) -> Bool {
        let home = NSHomeDirectory()
        let paths = ["/usr/local/bin/\(name)", "/opt/homebrew/bin/\(name)",
                     "\(home)/.local/bin/\(name)", "\(home)/.\(name)"]
        return paths.contains { FileManager.default.fileExists(atPath: $0) }
    }

    private func featureChip(_ icon: String, _ label: String) -> some View {
        HStack(spacing: 7) {
            Image(systemName: icon).font(.system(size: 13, weight: .medium))
            Text(label).font(.subheadline)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(Color.white.opacity(0.07))
        .clipShape(Capsule())
        .foregroundStyle(.white.opacity(0.85))
    }

    private func phraseChip(_ label: String) -> some View {
        Text("“\(label)”")
            .font(.caption.weight(.medium))
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(Color.white.opacity(0.06))
            .clipShape(Capsule())
            .foregroundStyle(.white.opacity(0.72))
    }

    private func request(_ kind: KeylessPermission) {
        pending.insert(kind)
        PermissionGuideController.shared.allowInline(kind)
        toast = "\(kind.title) is set up for Keyless. If macOS asks, choose Quit & Reopen — Keyless comes back here."
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            withAnimation {
                toast = nil
                pending.remove(kind)
            }
        }
    }

    private func toastBanner(_ message: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Tokens.Dark.success)
            Text(message).font(.subheadline)
            Spacer()
            Button { toast = nil } label: {
                Image(systemName: "xmark").foregroundStyle(.white.opacity(0.5))
            }
            .buttonStyle(.plain)
        }
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
        .padding(.horizontal, 70)
        .padding(.bottom, 84)
    }

    private var setupStatus: String {
        guard let active = flow.activeSetup else { return "Requesting access…" }
        return "\(flow.setupIndex + 1)/\(flow.setupTotal) · \(active.title)"
    }

    private var restartHint: String? {
        switch flow.activeSetup {
        case .accessibility: return "Already enabled it? Restart Keyless to apply the grant."
        case .screenRecording: return "Switch on Keyless, then restart the app to apply."
        default: return nil
        }
    }
}

// MARK: - Shared onboarding styles

private struct StepItem: Identifiable {
    let index: Int
    let step: SetupStep
    var id: String { step.rawValue }
}

private extension View {
    func cardStyle() -> some View {
        self
            .padding(18)
            .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(Color.white.opacity(0.10), lineWidth: 1)
            )
    }
}

private struct SetupCheck: Identifiable {
    let id: String
    let title: String
    let done: Bool
}

// MARK: - Permission card

private struct PermissionCard: View {
    let kind: KeylessPermission
    let grant: PermissionGrant
    let optional: Bool
    let waiting: Bool
    let onAllow: () -> Void

    var body: some View {
        HStack(spacing: 16) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(tint.opacity(0.18))
                    .frame(width: 42, height: 42)
                Image(systemName: icon)
                    .font(.system(size: 18, weight: .medium))
                    .foregroundStyle(tint)
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(kind.title).font(.headline)
                    if optional {
                        Text("Optional")
                            .font(.caption2)
                            .foregroundStyle(.white.opacity(0.4))
                    }
                }
                Text(purpose).font(.subheadline).foregroundStyle(.white.opacity(0.5))
            }
            Spacer()
            if grant == .granted {
                Label("Allowed", systemImage: "checkmark.circle.fill")
                    .font(.subheadline)
                    .foregroundStyle(Tokens.Dark.success)
            } else if waiting {
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for the switch").font(.caption).foregroundStyle(.white.opacity(0.6))
                }
            } else {
                Button("Allow", action: onAllow)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 13)
    }

    private var purpose: String {
        switch kind {
        case .camera: return "Locate your eyes for gaze tracking."
        case .microphone: return "Hear voice commands and pop-clicks."
        case .accessibility: return "Move the pointer and perform clicks and keys."
        case .screenRecording: return "Let your assistant see on-screen context."
        }
    }

    private var icon: String {
        switch kind {
        case .camera: return "camera.fill"
        case .microphone: return "mic.fill"
        case .accessibility: return "hand.point.up.left.fill"
        case .screenRecording: return "rectangle.on.rectangle"
        }
    }

    private var tint: Color {
        switch kind {
        case .camera, .microphone: return Tokens.Dark.accent
        case .accessibility: return Tokens.Dark.accent
        case .screenRecording: return Tokens.Dark.warning
        }
    }
}
