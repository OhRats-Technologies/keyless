// AudioLevelMonitor: a visualization-only microphone tap for the Home view.
//
// The Python engine owns all real listening and speech recognition. This
// monitor measures the input level just enough to animate the live waveform;
// it never records, stores, or forwards any audio, and muting simply stops
// feeding levels into the UI.

import AVFoundation
import Combine
import Foundation
import SwiftUI

@MainActor
final class AudioLevelMonitor: ObservableObject {
    /// A rolling window of recent input levels (oldest first, newest last),
    /// each in `0...1`. The waveform renders this directly so speech produces
    /// a real, scrolling envelope instead of a repeating animation.
    @Published private(set) var levels: [CGFloat]
    @Published private(set) var isMuted = false
    @Published private(set) var permissionGranted = false
    @Published private(set) var isMonitoring = false

    /// One-line caption from the on-device recognizer, plus whether that
    /// recognizer is authorized and available on this Mac.
    @Published private(set) var transcript = ""
    @Published private(set) var transcriptionAvailable = false

    /// Shared waveform geometry: one bar per tick across a fixed window. The
    /// caption's idle timeout uses the same window as the visible history.
    static let samplesPerSecond: Double = 30
    static let sampleCount = 72
    static let barSpacing: CGFloat = 3
    static var windowSeconds: Double { Double(sampleCount) / samplesPerSecond }

    private let audioEngine = AVAudioEngine()
    private var refreshTimer: Timer?
    private var hasTap = false

    private let lock = NSLock()
    private var rawLevel: Float = 0

    private var envelope: CGFloat = 0
    private var history: [CGFloat]

    private let transcriberBox = TranscriberBox()
    private var transcriber: LiveTranscriber?
    private var lastVoiceAt: Date?

    private let mutedKey = "keyless.microphone.muted"

    init() {
        let empty = Array(repeating: CGFloat(0), count: Self.sampleCount)
        history = empty
        levels = empty
        isMuted = UserDefaults.standard.bool(forKey: mutedKey)
    }

    deinit {
        refreshTimer?.invalidate()
        if audioEngine.isRunning { audioEngine.stop() }
    }

    // MARK: - lifecycle

    /// Called when the Home view appears. Asking for access here (rather than
    /// at app launch) keeps the permission prompt tied to a visible reason.
    func startMonitoring() {
        guard !isMonitoring else { return }
        isMonitoring = true

        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            Task { @MainActor in
                guard let self else { return }
                self.permissionGranted = granted
                if granted {
                    self.startTap()
                    Task { @MainActor in await self.startTranscription() }
                }
            }
        }

        refreshTimer = Timer.scheduledTimer(
            withTimeInterval: 1.0 / Self.samplesPerSecond, repeats: true
        ) { [weak self] _ in
            Task { @MainActor in self?.sample() }
        }
    }

    /// Called when the Home view disappears so the tap only lives while the
    /// waveform is on screen. The mute preference itself is persisted.
    func stopMonitoring() {
        refreshTimer?.invalidate()
        refreshTimer = nil
        if audioEngine.isRunning { audioEngine.stop() }
        if hasTap {
            audioEngine.inputNode.removeTap(onBus: 0)
            hasTap = false
        }
        lock.lock()
        rawLevel = 0
        lock.unlock()
        clearHistory()
        stopTranscription()
        isMonitoring = false
    }

    /// Re-probes permission after the user changes it in System Settings.
    func refreshPermission() {
        let granted = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        permissionGranted = granted
        if granted { startTap() }
    }

    // MARK: - mute

    func toggleMute() { setMuted(!isMuted) }

    func setMuted(_ muted: Bool) {
        isMuted = muted
        UserDefaults.standard.set(muted, forKey: mutedKey)
        if muted {
            clearHistory()
            stopTranscription()
        } else if isMonitoring {
            Task { @MainActor in await self.startTranscription() }
        }
    }

    // MARK: - transcription

    private func startTranscription() async {
        guard transcriber == nil, !isMuted else { return }
        guard await LiveTranscriber.requestAuthorization(),
              let transcriber = LiveTranscriber() else {
            transcriptionAvailable = false
            return
        }
        self.transcriber = transcriber
        transcriberBox.set(transcriber)
        transcriptionAvailable = true
        transcriber.start { [weak self] text, isFinal in
            Task { @MainActor in self?.setTranscript(text, isFinal: isFinal) }
        }
    }

    private func stopTranscription() {
        transcriberBox.set(nil)
        transcriber?.stop()
        transcriber = nil
        lastVoiceAt = nil
        transcript = ""
    }

    /// Show the latest recognized text directly; it clears once the mic has
    /// been quiet for one waveform window.
    private func setTranscript(_ text: String, isFinal _: Bool) {
        transcript = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !transcript.isEmpty { lastVoiceAt = Date() }
    }

    // MARK: - internals

    private func startTap() {
        guard !hasTap else { return }
        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else { return }
        let box = transcriberBox

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            box.append(buffer)
            guard let self, let data = buffer.floatChannelData else { return }
            let frames = Int(buffer.frameLength)
            guard frames > 0 else { return }
            var sum: Float = 0
            for i in 0..<frames {
                let sample = data[0][i]
                sum += sample * sample
            }
            let rms = sqrtf(sum / Float(frames))
            self.lock.lock()
            self.rawLevel = rms
            self.lock.unlock()
        }
        hasTap = true

        do {
            audioEngine.prepare()
            try audioEngine.start()
        } catch {
            input.removeTap(onBus: 0)
            hasTap = false
            permissionGranted = false
        }
    }

    /// Convert each sample to a display level and push it into the rolling
    /// history. A dB scale keeps quiet speech and loud speech both readable,
    /// and a fast attack / slower release makes the envelope feel responsive.
    private func sample() {
        lock.lock()
        let raw = rawLevel
        lock.unlock()

        let target: CGFloat
        if isMuted {
            target = 0
        } else {
            let gated = max(raw - 0.004, 0.00001)
            let db = 20.0 * log10(Double(gated))
            target = CGFloat(max(0.0, min(1.0, (db + 60.0) / 45.0)))
        }

        let factor: CGFloat = target > envelope ? 0.75 : 0.32
        envelope += (target - envelope) * factor
        if envelope < 0.005 { envelope = 0 }

        updateCaptionForSilence(voicePresent: target > 0.25)

        history.append(envelope)
        if history.count > Self.sampleCount {
            history.removeFirst(history.count - Self.sampleCount)
        }
        levels = history
    }

    private func updateCaptionForSilence(voicePresent: Bool) {
        let now = Date()
        if voicePresent { lastVoiceAt = now }
        guard !transcript.isEmpty, let last = lastVoiceAt,
              now.timeIntervalSince(last) > Self.windowSeconds else { return }
        transcript = ""
    }

    private func clearHistory() {
        envelope = 0
        history = Array(repeating: 0, count: Self.sampleCount)
        levels = history
    }
}
