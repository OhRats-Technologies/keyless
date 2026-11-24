// Rows.swift: the six native renderers for the engine-owned contract row kinds
// (status, action, permission, progress, picker, text) plus the generic
// SectionListView. The shell adds layout and theme only; title, detail, state,
// gating, and copy come verbatim from GET /v1/ui/state. An unknown row kind
// fails to decode in ContractModels, so drift is loud, never silent.

import SwiftUI

/// Posts the row's action (plus key/value for pickers) back to the engine.
typealias UiActionHandler = (ContractRow) -> Void

struct SectionListView: View {
    let sections: [ContractSection]
    let onAction: UiActionHandler

    var body: some View {
        ForEach(Array(sections.enumerated()), id: \.offset) { _, section in
            Section {
                ForEach(Array(section.rows.enumerated()), id: \.offset) { _, row in
                    ContractRowView(row: row, onAction: onAction)
                }
            } header: {
                Text(section.title)
            }
        }
    }
}

struct ContractRowView: View {
    let row: ContractRow
    let onAction: UiActionHandler
    @Environment(\.colorScheme) private var scheme

    private var theme: Theme { Theme(scheme) }

    var body: some View {
        switch row.kind {
        case .status: statusRow
        case .text: textRow
        case .action: actionRow
        case .permission: permissionRow
        case .progress: progressRow
        case .picker: pickerRow
        }
    }

    // MARK: status

    private var statusRow: some View {
        LabeledContent {
            HStack(spacing: 7) {
                Circle().fill(stateColor).frame(width: 8, height: 8)
                Text(row.detail ?? "").foregroundStyle(.secondary)
            }
        } label: {
            Text(row.title)
        }
    }

    private var stateColor: Color {
        switch row.state {
        case "ok", "granted": return theme.success
        case "warn", "pending": return theme.warning
        case "error", "denied": return theme.danger
        default: return theme.muted
        }
    }

    // MARK: text

    private var textRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(row.title)
            if let detail = row.detail, !detail.isEmpty {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    // MARK: action

    private var actionRow: some View {
        Button {
            onAction(row)
        } label: {
            Text(row.title)
        }
        .buttonStyle(row.style == "primary" ? .borderedProminent : .bordered)
        .disabled(row.enabled == false)
    }

    // MARK: permission

    private var permissionRow: some View {
        LabeledContent {
            HStack(spacing: 7) {
                Circle().fill(stateColor).frame(width: 8, height: 8)
                Text(permissionDetail).foregroundStyle(.secondary)
            }
        } label: {
            Text(row.title)
        }
    }

    private var permissionDetail: String {
        switch row.state {
        case "granted": return "Granted"
        case "denied": return "Denied"
        default: return row.optional == true ? "Optional" : "Needs attention"
        }
    }

    // MARK: progress

    private var progressRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(row.title)
            if row.indeterminate == true {
                ProgressView().progressViewStyle(.linear)
            } else {
                ProgressView(value: max(0, min(1, row.fraction ?? 0)))
                    .progressViewStyle(.linear)
            }
            if let detail = row.detail, !detail.isEmpty {
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    // MARK: picker

    private var pickerRow: some View {
        Picker(row.title, selection: pickerSelection) {
            ForEach(Array((row.options ?? []).enumerated()), id: \.offset) { _, option in
                Text(option.label).tag(option.value ?? "")
            }
        }
    }

    /// Choosing an option posts the row's own action with ``key: value``; the
    /// engine applies it and the next document reflects the new selection.
    private var pickerSelection: Binding<String> {
        Binding(
            get: { row.selected ?? "" },
            set: { value in
                var changed = row
                changed.selected = value
                onAction(changed)
            }
        )
    }
}

/// Builds the ``POST /v1/ui/action`` payload a row requests. Picker rows carry
/// their own ``action`` and ``key``; permission rows post ``allow`` with the
/// permission id; other rows post their action id alone.
func contractActionPayload(for row: ContractRow) -> [String: String] {
    var payload: [String: String] = [:]
    switch row.kind {
    case .picker:
        payload["action"] = row.action ?? "set_config"
        if let key = row.key { payload[key] = row.selected ?? "" }
    case .permission:
        payload["action"] = row.action ?? "allow"
        if let id = row.id { payload["permission"] = id }
    default:
        payload["action"] = row.action ?? ""
    }
    return payload
}
