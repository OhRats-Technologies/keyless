// ContractModels.swift: Decodable mirror of the engine-owned schema-v1 UI
// document (`GET /v1/ui/state`). The shell renders these models verbatim;
// it never re-derives screens, gating, or copy. Row kinds are a closed set of
// six (docs/plans/ui-unification-goal.md); an unknown kind fails to decode so
// shell/engine schema drift is loud, never silent.

import Foundation

enum ContractRowKind: String, Decodable {
    case status, action, permission, progress, picker, text
}

struct ContractOption: Decodable, Hashable {
    var label: String
    var value: String?
}

struct ContractRow: Decodable {
    var kind: ContractRowKind
    var title: String

    // status / text
    var detail: String?
    var state: String?

    // action
    var action: String?
    var style: String?
    var enabled: Bool?

    // permission
    var id: String?
    var optional: Bool?
    var needsRestart: Bool?

    // picker. ``selected`` is nullable: an unset config value (e.g. "System
    // default" audio device) round-trips as JSON null.
    var key: String?
    var options: [ContractOption]?
    var selected: String?

    // progress
    var fraction: Double?
    var indeterminate: Bool?
}

struct ContractSection: Decodable {
    var title: String
    var rows: [ContractRow]
}

struct ContractScreen: Decodable {
    var title: String
    var sections: [ContractSection]

    // setup only
    var complete: Bool?
    var current: String?
    var steps: [String]?
}

struct ContractActionSpec: Decodable {
    var id: String
    var method: String
    var path: String
}

struct UIStateDocument: Decodable {
    var schemaVersion: Int
    var actions: [String: ContractActionSpec]
    var screens: [String: ContractScreen]

    var home: ContractScreen? { screens["home"] }
    var activity: ContractScreen? { screens["activity"] }
    var settings: ContractScreen? { screens["settings"] }
    var setup: ContractScreen? { screens["setup"] }
}
