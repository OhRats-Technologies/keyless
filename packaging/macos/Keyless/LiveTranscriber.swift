// LiveTranscriber: on-device partial captions for the Home view.
//
// This is captioning for the waveform only. The Python engine still owns all
// command recognition; nothing here routes or records speech. We require
// on-device recognition so audio never leaves the Mac.

import AVFoundation
import Speech

final class LiveTranscriber {
    private let recognizer: SFSpeechRecognizer?
    private let queue = DispatchQueue(label: "party.ohrats.Keyless.transcriber")
    private let lock = NSLock()

    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var onResult: ((String, Bool) -> Void)?

    init?() {
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.supportsOnDeviceRecognition else { return nil }
        self.recognizer = recognizer
    }

    static func requestAuthorization() async -> Bool {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }

    func start(onResult: @escaping (String, Bool) -> Void) {
        stop()

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true
        request.taskHint = .dictation

        lock.lock()
        self.onResult = onResult
        self.request = request
        lock.unlock()

        let task = recognizer?.recognitionTask(with: request) { [weak self] result, error in
            guard let self, let result, error == nil else { return }
            let text = result.bestTranscription.formattedString
            let isFinal = result.isFinal
            self.lock.lock()
            let callback = self.onResult
            self.lock.unlock()
            DispatchQueue.main.async { callback?(text, isFinal) }
        }
        lock.lock()
        self.task = task
        lock.unlock()
    }

    /// Called off the audio tap thread; appends are serialized so they can
    /// never race with `endAudio()` from `stop()`.
    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        let request = self.request
        lock.unlock()
        queue.async { request?.append(buffer) }
    }

    func stop() {
        lock.lock()
        let request = self.request
        let task = self.task
        self.request = nil
        self.task = nil
        self.onResult = nil
        lock.unlock()
        queue.async {
            request?.endAudio()
            task?.cancel()
        }
    }
}

/// Thread-safe handoff from the mic tap to the transcriber. The tap is
/// installed while transcription may still be awaiting authorization.
final class TranscriberBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: LiveTranscriber?

    func set(_ transcriber: LiveTranscriber?) {
        lock.lock()
        value = transcriber
        lock.unlock()
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        let transcriber = value
        lock.unlock()
        transcriber?.append(buffer)
    }
}
