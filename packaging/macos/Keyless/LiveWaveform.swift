// LiveWaveform: a scrolling audio envelope.
//
// Each bar is one recent sample from AudioLevelMonitor, so the shape is the
// measured history of what the microphone heard rather than a decorative
// animation. Newest samples sit on the right.

import SwiftUI

struct LiveWaveform: View {
    let levels: [CGFloat]
    let isMuted: Bool
    var tint: Color = .accentColor

    var body: some View {
        Canvas { context, size in
            draw(in: size, into: &context)
        }
        .accessibilityHidden(true)
    }

    private func draw(in size: CGSize, into context: inout GraphicsContext) {
        guard size.width > 0, size.height > 0, !levels.isEmpty else { return }

        let count = levels.count
        let spacing = AudioLevelMonitor.barSpacing
        let barWidth = max(1.5, (size.width - spacing * CGFloat(count - 1)) / CGFloat(count))
        let centerY = size.height / 2
        let maxHeight = size.height - 2
        let color: Color = isMuted ? .secondary : tint

        for (i, sample) in levels.enumerated() {
            let value = isMuted ? 0 : min(1, max(0, sample))
            let height = max(2, value * maxHeight)
            let rect = CGRect(
                x: CGFloat(i) * (barWidth + spacing),
                y: centerY - height / 2,
                width: barWidth,
                height: height
            )
            let opacity = isMuted ? 0.30 : 0.45 + 0.55 * value
            context.fill(
                Path(roundedRect: rect, cornerRadius: barWidth / 2),
                with: .color(color.opacity(opacity))
            )
        }
    }
}
