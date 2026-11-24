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
    var model: ModelState
}

struct SetupStepState: Decodable {
    var step: String
    var completed: [String]
    var skipped: [String]
    var unresolved: [String]
    var updatedAt: String
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
    @Published var ui: UIStateDocument?

    var setupComplete: Bool { setup?.complete ?? false }
    var modelReady: Bool { setup?.model.ready ?? false }

    var menuIcon: String {
        switch state {
        case "listening": return "circle.fill"
        case "paused": return "pause.circle"
        case "error": return "exclamationmark.circle"
        default: return "circle"
        }
    }

    private var timer: Timer?
    private var eventsTask: Task<Void, Never>?
    private var eventsLive = false

    init(baseURL: URL) {
        self.baseURL = baseURL
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, !self.eventsLive else { return }
                await self.refresh()
                await self.refreshUI()
            }
        }
        Task {
            await refresh()
            await refreshUI()
        }
        startEventStream()
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

    // MARK: Contract document and actions

    /// Refetch the engine-owned UI contract (`GET /v1/ui/state`). Every shell
    /// screen renders from this document; unknown row kinds fail to decode.
    func refreshUI() async {
        ui = (try? await get("ui/state")) ?? ui
    }

    /// Post a contract action (`POST /v1/ui/action`) and refetch. The payload
    /// is built by the row renderer from the row's own action/key/id fields,
    /// so new contract rows need no shell code.
    func uiAction(_ payload: [String: String]) async {
        _ = try? await postJSON("ui/action", payload)
        await refreshUI()
    }

    // MARK: Live events (SSE with polling fallback)

    private var eventsTask: Task<Void, Never>?
    private var eventsLive = false

    /// Consume ``GET /v1/events`` and refetch on every ``state`` event. While
    /// the stream is healthy the 1 s poll timer is skipped; on any break the
    /// flag flips back and polling covers until the stream reconnects.
    func startEventStream() {
        guard eventsTask == nil else { return }
        eventsTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.consumeEvents()
                if Task.isCancelled { return }
                self.eventsLive = false
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private func consumeEvents() async {
        let url = baseURL.appending(path: "/v1/events")
        var req = URLRequest(url: url)
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        guard let (bytes, _) = try? await URLSession.shared.bytes(for: req) else { return }
        eventsLive = true
        do {
            for try await line in bytes.lines {
                if Task.isCancelled { return }
                if line.hasPrefix("data:") {
                    await refresh()
                    await refreshUI()
                }
            }
        } catch {}
    }

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
