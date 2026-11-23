// PermissionGuide: TCC probes and grant actions for the immersive onboarding.
//
// Keyless asks only for what it needs: Camera, Microphone, Accessibility, and
// (optionally) Screen Recording. Like Hex, Accessibility covers the event taps
// our hotkeys use, so Input Monitoring is never requested. Grants are read
// directly from the OS each time; there is no cached state or follow panel.

import AppKit
import AVFoundation
import Darwin

enum KeylessPermission: Int, CaseIterable {
    case accessibility = 1
    case microphone = 2
    case camera = 5
    case screenRecording = 4

    var title: String {
        switch self {
        case .accessibility: return "Accessibility"
        case .microphone: return "Microphone"
        case .camera: return "Camera"
        case .screenRecording: return "Screen Recording"
        }
    }

    private var pane: String {
        switch self {
        case .accessibility: return "Privacy_Accessibility"
        case .microphone: return "Privacy_Microphone"
        case .camera: return "Privacy_Camera"
        case .screenRecording: return "Privacy_ScreenCapture"
        }
    }

    var settingsURL: URL {
        URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?\(pane)")!
    }
}

enum PermissionGrant: Equatable {
    case granted, denied, notAsked, unknown
}

@MainActor
final class PermissionGuideController {
    static let shared = PermissionGuideController()
    private init() {}

    /// Fire only the native TCC prompt, with no Settings pane. Returns false
    /// when this permission has no native prompt (Screen Recording).
    @discardableResult
    func promptOnly(_ kind: KeylessPermission) -> Bool {
        switch kind {
        case .accessibility:
            let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(opts)
        case .microphone:
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
        case .camera:
            AVCaptureDevice.requestAccess(for: .video) { _ in }
        case .screenRecording:
            return false
        }
        return true
    }

    /// Onboarding row action: native prompt where one exists, otherwise open
    /// the exact Settings pane. No panel or overlay is raised.
    func allowInline(_ kind: KeylessPermission) {
        if !promptOnly(kind) {
            NSWorkspace.shared.open(kind.settingsURL)
        }
    }

    /// Some TCC grants only apply to a fresh process. Restart cleanly: the
    /// single-instance lock FD closes with us and prefs are flushed first.
    func relaunch() {
        let path = Bundle.main.bundleURL.path
        Task.detached {
            // One plain `open` after we exit — LaunchServices starts exactly
            // one instance. `open -n` would race the lock.
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/sh")
            proc.arguments = ["-c", "sleep 1; open \"\(path)\""]
            try? proc.run()
            await MainActor.run {
                UserDefaults.standard.synchronize()
                exit(0)
            }
        }
    }

    /// Drop every TCC grant for this bundle and relaunch so the OS asks again.
    func revokeAllPermissions() {
        let bundleID = Bundle.main.bundleIdentifier ?? "party.ohrats.Keyless"
        Task.detached {
            for service in ["Accessibility", "ScreenCapture", "Microphone", "Camera", "ListenEvent"] {
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/tccutil")
                proc.arguments = ["reset", service, bundleID]
                try? proc.run()
                proc.waitUntilExit()
            }
            await MainActor.run { self.relaunch() }
        }
    }

    /// Live TCC state for one permission.
    func currentGrant(_ kind: KeylessPermission) -> PermissionGrant {
        switch kind {
        case .accessibility:
            // Hex treats either generic AX trust or the newer post-event TCC
            // service as ready: both let us create the hotkey/input taps.
            return (AXIsProcessTrusted() || CGPreflightPostEventAccess()) ? .granted : .denied
        case .microphone, .camera:
            let media: AVMediaType = kind == .microphone ? .audio : .video
            switch AVCaptureDevice.authorizationStatus(for: media) {
            case .authorized: return .granted
            case .denied, .restricted: return .denied
            case .notDetermined: return .notAsked
            @unknown default: return .unknown
            }
        case .screenRecording:
            return CGPreflightScreenCaptureAccess() ? .granted : .denied
        }
    }
}
