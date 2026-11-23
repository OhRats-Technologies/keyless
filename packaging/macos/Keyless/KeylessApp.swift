// Keyless macOS native host — SwiftUI entry point.
//
// Cross-platform contract: the Python engine owns gaze/voice/routing and
// exposes a loopback control plane (`keyless serve`, default
// http://127.0.0.1:8766). This app is a thin host: MenuBarExtra status,
// Home/Activity/Settings, and the engine-owned setup flow. It never bundles
// the engine — it drives it.
//
// Build: open in Xcode 15+ on macOS 14+, or `swift build` via Package.swift.
// Signing: Developer ID + notarization, hardened runtime (see README.md).
// TCC usage strings live in Info.plist.

import Darwin
import SwiftUI

/// One window, always. A file lock makes a second launch quit instead of
/// opening another copy of the UI.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var lockFD: Int32 = -1

    func applicationDidFinishLaunching(_ notification: Notification) {
        let dir = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        )[0].appendingPathComponent("Keyless")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let path = dir.appendingPathComponent("keyless-app.lock").path
        lockFD = open(path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            DispatchQueue.main.async { NSApp.terminate(nil) }
            return
        }

        // Handle the raw `quit` AppleEvent ourselves. macOS's "Quit & Reopen"
        // button (shown after granting Accessibility) sends this while setup
        // is on screen. AppKit's default handler can route the terminate to a
        // presented dialog's cancel button, so the app never actually quits
        // and the OS never relaunches it with the fresh TCC grant. Intercepting
        // the event first lets us exit cleanly;
        // all state here is disposable (lock FD closes, prefs are flushed).
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleQuitEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kCoreEventClass),
            andEventID: AEEventID(kAEQuitApplication)
        )
    }

    @objc private func handleQuitEvent(
        _ event: NSAppleEventDescriptor,
        withReplyEvent replyEvent: NSAppleEventDescriptor
    ) {
        UserDefaults.standard.synchronize()
        exit(0)
    }
}

@main
struct KeylessApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var engine = EngineBridge(baseURL: URL(string: "http://127.0.0.1:8766")!)
    @StateObject private var audio = AudioLevelMonitor()
    @AppStorage("keyless.onboarding.complete") private var onboardingComplete = false
    @State private var showingOnboarding = false

    init() {
        // AppKit restores an open sheet on relaunch, which can resurrect an
        // onboarding tour the user already dismissed with "Not now". Drop
        // those restoration keys; the saved flag remains the source of truth.
        let defaults = UserDefaults.standard
        for key in defaults.dictionaryRepresentation().keys
        where key.contains("SheetPresentationModifier") {
            defaults.removeObject(forKey: key)
        }
    }

    var body: some Scene {
        WindowGroup("Keyless") {
            ContentView(engine: engine, audio: audio, showingOnboarding: $showingOnboarding)
                .onAppear {
                    // The engine owns the setup state file (shared with the
                    // Linux shell). The local flag is only an offline cache.
                    Task { @MainActor in
                        await engine.refresh()
                        showingOnboarding = !(engine.setup?.complete ?? onboardingComplete)
                    }
                }
                .onChange(of: showingOnboarding) { _, showing in
                    if !showing { onboardingComplete = true }
                }
        }
        .windowStyle(.titleBar)
        .commands {
            CommandGroup(replacing: .appInfo) {
                Button("About Keyless") { NSApp.orderFrontStandardAboutPanel() }
            }
            CommandGroup(after: .appSettings) {
                Button("Run setup…") { showingOnboarding = true }
            }
        }

        MenuBarExtra("Keyless", systemImage: engine.menuIcon) {
            MenuBarView(engine: engine, showingOnboarding: $showingOnboarding)
        }
    }
}
