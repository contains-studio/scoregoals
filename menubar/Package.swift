// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "DayloopBar",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "DayloopBar",
            path: "Sources/DayloopBar"
        )
    ]
)
