// EngineBridge: URLSession client for the Python loopback control plane.
//
// The engine is the single source of truth for setup, status, config and
// diagnostics. This bridge only transports its JSON — no shell-side copies of
// step order, calibration state, or copy.

import Combine
import Foundation

struct EngineStatus: Decodable {
    var state: String
    var detail: String
}

struct HistoryAction: Decodable, Identifiable {
    var id: Int
    var transcript: String
    var route: String
    var target: String
    var result: String
}

/// The engine-owned onboarding document (`GET /v1/setup`). Both the macOS and
/// Linux shells drive the same ordered steps from this payload.
struct SetupState: Decodable {
    var schemaVersion: Int
    var steps: [String]
    var state: SetupStepState
    var complete: Bool
    var calibration: CalibrationState
    var model: ModelState
}

struct SetupStepState: Decodable {
    var step: String
    var completed: [String]
    var skipped: [String]
    var unresolved: [String]
    var updatedAt: String
}

struct CalibrationState: Decodable {
    var path: String
    var ready: Bool
    var running: Bool
}

struct ModelState: Decodable {
    var id: String
    var name: String
    var displayName: String
    var size: Int
    var state: String
    var ready: Bool
    var selected: String
    var job: ModelJob?
}

struct RuntimeInfo: Decodable {
    var python: String
    var args: [String]
    var cwd: String
}

struct ModelJob: Decodable {
    var state: String
    var percent: Double
    var bytesDone: Int
    var bytesTotal: Int
    var fileName: String
    var fileIndex: Int
    var fileCount: Int
    var error: String
}

@MainActor
final class EngineBridge: ObservableObject {
    let baseURL: URL
    @Published var state: String = "stopped"
    @Published var detail: String = "Not connected to `keyless serve`."
    @Published var recent: [HistoryAction] = []
    @Published var config: [String: Any] = [:]
    @Published var doctor: [String: Any] = [:]
    @Published var setup: SetupState?
    @Published var model: ModelState?
    @Published var runtime: RuntimeInfo?

    var setupComplete: Bool { setup?.complete ?? false }
    var calibrationReady: Bool { setup?.calibration.ready ?? false }
    var modelReady: Bool { model?.ready ?? false }

    var menuIcon: String {
        switch state {
        case "listening": return "circle.fill"
        case "paused": return "pause.circle"
        case "error": return "exclamationmark.circle"
        default: return "circle"
        }
    }

    private var timer: Timer?

    init(baseURL: URL) {
        self.baseURL = baseURL
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
        Task { await refresh() }
    }

    func refresh() async {
        if let status: EngineStatus = try? await get("status") {
            state = status.state
            detail = status.detail
        }
        if let page: HistoryPage = try? await get("history?limit=5") {
            recent = page.actions
        }
        config = (try? await getJSON("config")) ?? config
        doctor = (try? await getJSON("doctor")) ?? doctor
        setup = (try? await get("setup")) ?? setup
        model = setup?.model
        runtime = (try? await get("runtime")) ?? runtime
    }

    func start() async { await post("start") }
    func pause() async { await post("pause") }

    /// Advance/restart the engine-owned setup state machine.
    func setupAction(_ action: String, step: String? = nil) async {
        var payload: [String: String] = ["action": action]
        if let step { payload["step"] = step }
        _ = try? await postJSON("setup", payload)
        await refresh()
    }

    /// The engine owns one verified background download; this only starts or
    /// stops it and renders the progress it publishes.
    func downloadModel() async {
        _ = try? await postJSON("models/download", [:])
        await refresh()
    }

    func cancelModelDownload() async {
        _ = try? await postJSON("models/cancel", [:])
        await refresh()
    }

    func saveConfig(_ patch: [String: Any]) async {
        var req = URLRequest(url: baseURL.appending(path: "/v1/config"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: patch)
        _ = try? await URLSession.shared.data(for: req)
        await refresh()
    }

    // MARK: - plumbing

    private struct HistoryPage: Decodable { var actions: [HistoryAction] }

    private func get<T: Decodable>(_ route: String) async throws -> T {
        let url = baseURL.appending(path: "/v1/\(route)")
        let (data, _) = try await URLSession.shared.data(from: url)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: data)
    }

    private func getJSON(_ route: String) async throws -> [String: Any] {
        let url = baseURL.appending(path: "/v1/\(route)")
        let (data, _) = try await URLSession.shared.data(from: url)
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }

    private func post(_ route: String) async {
        var req = URLRequest(url: baseURL.appending(path: "/v1/\(route)"))
        req.httpMethod = "POST"
        req.httpBody = Data()
        _ = try? await URLSession.shared.data(for: req)
        await refresh()
    }

    private func postJSON(_ route: String, _ payload: [String: String]) async throws -> Data {
        var req = URLRequest(url: baseURL.appending(path: "/v1/\(route)"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: payload)
        let (data, _) = try await URLSession.shared.data(for: req)
        return data
    }
}
