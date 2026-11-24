// ContractModelsTests: decode the shared golden fixtures that the Qt shell
// also renders (tests/fixtures/ui/). Both shells consume one contract, so the
// Swift decoder is pinned to the same fixtures the Python side pins.

import XCTest
@testable import Keyless

final class ContractModelsTests: XCTestCase {
    private func document(_ name: String) throws -> UIStateDocument {
        // Packaging lives at <repo>/packaging/macos; fixtures at <repo>/tests/fixtures/ui.
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // KeylessTests
            .deletingLastPathComponent() // Tests
            .deletingLastPathComponent() // macos
            .deletingLastPathComponent() // packaging
            .deletingLastPathComponent() // repo root
            .appendingPathComponent("tests/fixtures/ui/\(name).json")
        let data = try Data(contentsOf: root)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(UIStateDocument.self, from: data)
    }

    func testFullFixtureDecodes() throws {
        let doc = try document("contract_full")
        XCTAssertEqual(doc.schemaVersion, 1)
        for key in ["home", "activity", "settings", "setup"] {
            XCTAssertNotNil(doc.screens[key], "missing screen \(key)")
        }
        XCTAssertFalse(doc.actions.isEmpty)
        for (_, spec) in doc.actions {
            XCTAssertEqual(spec.method, "POST")
        }
    }

    func testSetupScreenCarriesEngineOwnedSteps() throws {
        let setup = try XCTUnwrap(document("contract_full").setup)
        XCTAssertEqual(setup.steps, ["welcome", "devices", "permissions", "ready"])
        XCTAssertNotNil(setup.current)
    }

    func testRowKindsAreWithinClosedSet() throws {
        // Decoding already fails on an unknown kind (enum has no fallback),
        // so a clean decode proves every fixture row is one of the six.
        let doc = try document("contract_full")
        var count = 0
        for (_, screen) in doc.screens {
            for section in screen.sections {
                count += section.rows.count
                for row in section.rows {
                    XCTAssertContains([ContractRowKind.status, .action, .permission,
                                       .progress, .picker, .text], row.kind)
                }
            }
        }
        XCTAssertGreaterThan(count, 0)
    }

    func testColdFixtureOmitsUnavailableRows() throws {
        let doc = try document("contract_cold")
        XCTAssertNotNil(doc.home)
        let kinds = (doc.settings?.sections.flatMap { $0.rows } ?? []).map(\.kind)
        XCTAssertFalse(kinds.contains(.progress), "cold machine has no downloads in flight")
    }

    private func XCTAssertContains<T: Equatable>(_ collection: [T], _ element: T) {
        XCTAssertTrue(collection.contains(element), "unexpected row kind \(element)")
    }
}
