import SwiftUI

/// One rate-limit window: `5h ▓▓▓░░ 31% left · resets 2h 10m`.
/// Fuel-gauge colors match the TUI thresholds (theme.py): red ≤10% left,
/// amber ≤30%, green otherwise.
struct UsageBar: View {
    let window: UsageWindow

    private var color: Color {
        guard let left = window.pctLeft else { return .secondary }
        if left <= 10 { return .red }
        if left <= 30 { return .orange }
        return .green
    }

    var body: some View {
        HStack(spacing: 6) {
            // Wide enough for model-scoped window ids like "fable", not just "5h"/"wk".
            Text(window.id)
                .font(.caption2.monospaced())
                .foregroundStyle(.secondary)
                .frame(width: 34, alignment: .leading)

            if let left = window.pctLeft {
                Capsule()
                    .fill(.quaternary)
                    .frame(width: 56, height: 5)
                    .overlay(alignment: .leading) {
                        Capsule()
                            .fill(color)
                            .frame(width: 56 * left / 100)
                    }
                Text("\(Int(left))% left")
                    .font(.caption2.monospaced())
                    .foregroundStyle(color)
                    .frame(width: 58, alignment: .leading)
                if !window.resetsIn.isEmpty {
                    Text("resets \(window.resetsIn)")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            } else {
                Text("no usage data")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
    }
}
