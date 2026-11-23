// PermissionsView: TCC rows with live state and one action each. The Allow
// button fires the native prompt where one exists, otherwise it opens the
// exact Privacy pane. No overlay or guide panel is involved.

import Combine
import SwiftUI

struct PermissionsView: View {
    private let guide = PermissionGuideController.shared
    @State private var grants: [KeylessPermission: PermissionGrant] = [:]
    private let timer = Timer.publish(every: 1.0, on: .main, in: .common).autoconnect()

    // Accessibility covers the event taps hotkeys need, so (like Hex) we do
    // not surface a separate Input Monitoring row.
    private let kinds: [KeylessPermission] = [
        .camera, .microphone, .screenRecording, .accessibility,
    ]

    var body: some View {
        ForEach(kinds, id: \.self) { kind in
            let grant = grants[kind] ?? guide.currentGrant(kind)
            HStack {
                Image(systemName: grant == .granted ? "checkmark.circle.fill"
                      : grant == .denied ? "exclamationmark.circle.fill" : "circle")
                    .foregroundStyle(grant == .granted ? .green : grant == .denied ? .red : .secondary)
                Text(kind.title)
                Spacer()
                Button(grant == .granted ? "Allowed" : grant == .denied ? "Fix" : "Allow") {
                    guide.allowInline(kind)
                }
                .buttonStyle(.link)
                .disabled(grant == .granted)
            }
            .padding(.vertical, 2)
        }
        .onReceive(timer) { _ in
            for kind in kinds { grants[kind] = guide.currentGrant(kind) }
        }
    }
}
