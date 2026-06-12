import SwiftUI

@main
struct CCCApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        MenuBarExtra {
            PopoverView()
                .environmentObject(state)
        } label: {
            HStack(spacing: 3) {
                Image(systemName: state.menuBarSymbol)
                if !state.menuBarTitle.isEmpty {
                    Text(state.menuBarTitle)
                }
            }
        }
        .menuBarExtraStyle(.window)
    }
}
