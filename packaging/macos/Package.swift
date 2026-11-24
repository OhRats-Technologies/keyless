// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Keyless",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "Keyless",
            path: "Keyless",
            exclude: ["Info.plist"]
        ),
        .testTarget(
            name: "KeylessTests",
            dependencies: ["Keyless"],
            path: "Tests/KeylessTests"
        ),
    ]
)
