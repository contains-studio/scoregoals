// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ScoreGoals",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "ScoreGoals",
            path: "Sources/ScoreGoals"
        )
    ]
)
