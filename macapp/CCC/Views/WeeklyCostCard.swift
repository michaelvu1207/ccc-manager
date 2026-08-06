import Charts
import SwiftUI

struct WeeklyCostCard: View {
    @EnvironmentObject private var state: AppState
    @State private var isExpanded = false

    private var model: WeeklyCostViewModel {
        WeeklyCostViewModel(
            snapshot: state.snapshot,
            isRefreshing: state.isRefreshing,
            errorMessage: state.errorMessage
        )
    }

    var body: some View {
        Group {
            switch model.state {
            case .loading:
                loadingView
            case .empty:
                emptyView
            case .content:
                contentView
            case .error(let message):
                errorView(message)
            }
        }
        .padding(8)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 8))
    }

    private var loadingView: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Past 7 days").font(.caption.weight(.semibold))
                Spacer()
                ProgressView().controlSize(.mini)
                    .accessibilityLabel("Loading seven-day costs")
            }
            HStack(alignment: .bottom, spacing: 6) {
                ForEach(0..<7, id: \.self) { index in
                    RoundedRectangle(cornerRadius: 2)
                        .fill(.quaternary)
                        .frame(height: CGFloat(12 + (index % 4) * 8))
                        .frame(maxWidth: .infinity, alignment: .bottom)
                }
            }
            .frame(height: 42, alignment: .bottom)
            .redacted(reason: .placeholder)
            .accessibilityHidden(true)
        }
    }

    private var emptyView: some View {
        HStack(spacing: 8) {
            Image(systemName: "chart.bar")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text("No usage in the past 7 days")
                    .font(.caption.weight(.semibold))
                Text("Claude and Codex activity will appear here")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .accessibilityElement(children: .combine)
    }

    private func errorView(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "chart.bar.xaxis")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text("Seven-day costs unavailable")
                    .font(.caption.weight(.semibold))
                Text(message)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer()
            if state.isRefreshing {
                ProgressView().controlSize(.mini)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var contentView: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            ScrollView {
                details
                    .padding(.top, 6)
            }
            .frame(maxHeight: 220)
        } label: {
            VStack(alignment: .leading, spacing: 7) {
                totals
                weeklyChart
                legend
            }
            .contentShape(Rectangle())
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Seven-day Claude and Codex cost")
            .accessibilityValue(model.accessibilitySummary)
            .accessibilityHint(isExpanded ? "Collapse cost details" : "Expand cost details")
        }
    }

    private var totals: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 0) {
                Text(model.weeklyCostText)
                    .font(.title3.weight(.semibold))
                    .monospacedDigit()
                Text("past 7 days")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 0) {
                Text(model.todayCostText)
                    .font(.callout.weight(.medium))
                    .monospacedDigit()
                Text("today")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var weeklyChart: some View {
        if model.chartPoints.isEmpty {
            HStack {
                Spacer()
                Text("Cost unavailable for recorded token activity")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            .frame(height: 58)
        } else {
            Chart(model.chartPoints) { point in
                BarMark(
                    x: .value("Day", point.date),
                    y: .value("Cost", point.cost)
                )
                .foregroundStyle(providerColor(point.provider))
                .opacity(point.status == "partial" ? 0.55 : 1)
                .cornerRadius(2)
            }
            .chartXAxis {
                AxisMarks(values: model.orderedDays) { value in
                    AxisValueLabel {
                        if let date = value.as(String.self) {
                            Text(model.weekday(for: date))
                                .font(.system(size: 9))
                        }
                    }
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading, values: .automatic(desiredCount: 2)) {
                    AxisGridLine().foregroundStyle(.quaternary)
                }
            }
            .frame(height: 74)
            // VoiceOver gets a concise semantic summary instead of dozens of marks.
            .accessibilityHidden(true)
        }
    }

    private var legend: some View {
        HStack(spacing: 12) {
            ForEach(model.providerOrder, id: \.self) { provider in
                if model.report?.providers[provider] != nil {
                    Label {
                        Text(model.providerName(provider))
                    } icon: {
                        Circle()
                            .fill(providerColor(provider))
                            .frame(width: 7, height: 7)
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
            }
            Spacer()
            if let report = model.report, report.status != "exact" {
                Text(report.status)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(report.status == "unknown" ? Color.gray : Color.orange)
            }
        }
    }

    @ViewBuilder
    private var details: some View {
        if let report = model.report {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(model.providerOrder, id: \.self) { provider in
                    if let bucket = report.providers[provider] {
                        metricRow(
                            title: model.providerName(provider),
                            subtitle: model.tokenBreakdown(bucket.tokens),
                            value: model.format(bucket: bucket),
                            color: providerColor(provider)
                        )
                    }
                }

                if !report.accounts.isEmpty {
                    Divider().padding(.vertical, 1)
                    Text("Accounts")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(report.accounts) { account in
                        metricRow(
                            title: account.displayName,
                            subtitle: "\(model.providerName(account.provider)) · \(model.tokenBreakdown(account.tokens))",
                            value: model.format(bucket: account.bucket),
                            color: providerColor(account.provider)
                        )
                    }
                }

                if !report.models.isEmpty {
                    Divider().padding(.vertical, 1)
                    Text("Models")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(report.models) { item in
                        metricRow(
                            title: item.model,
                            subtitle: "\(model.providerName(item.provider)) · \(model.tokenBreakdown(item.tokens))",
                            value: model.format(bucket: item.bucket),
                            color: providerColor(item.provider)
                        )
                    }
                }

                Text(model.qualityLabel(report.status))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .padding(.top, 2)
            }
        }
    }

    private func metricRow(
        title: String, subtitle: String, value: String, color: Color
    ) -> some View {
        HStack(spacing: 6) {
            RoundedRectangle(cornerRadius: 1)
                .fill(color)
                .frame(width: 3, height: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 0) {
                Text(title).font(.caption).lineLimit(1).truncationMode(.middle)
                Text(subtitle).font(.caption2).foregroundStyle(.secondary)
            }
            Spacer()
            Text(value).font(.caption.monospacedDigit())
        }
        .accessibilityElement(children: .combine)
    }

    private func providerColor(_ provider: String) -> Color {
        switch provider {
        case "claude": return .orange
        case "codex": return .blue
        default: return .secondary
        }
    }
}
