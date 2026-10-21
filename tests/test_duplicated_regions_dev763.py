"""DEV-763: tell the implementer when a file it must edit repeats a region."""
from coding_model_autonomous import messages
from coding_model_autonomous.apply_edits import (
    describe_duplicated_regions, duplicated_regions)

COORDINATOR = """\
    final class Coordinator {
        var parent: MetalViewRepresentable
        var renderer: Renderer?
        init(_ parent: MetalViewRepresentable) { self.parent = parent }
        func draw(in view: MTKView) {
            guard let renderer = renderer else { return }
            renderer.render(into: view.currentDrawable, size: view.drawableSize)
        }
    }
"""

# Run 47's file: the nested Coordinator is byte-identical under both branches.
RUN47 = (
    "import SwiftUI\n#if os(macOS)\n"
    "struct MetalViewRepresentable: NSViewRepresentable {\n"
    "    func makeNSView(context: Context) -> MTKView { MTKView() }\n"
    + COORDINATOR +
    "}\n#elseif os(iOS) || os(visionOS)\n"
    "struct MetalViewRepresentable: UIViewRepresentable {\n"
    "    func makeUIView(context: Context) -> MTKView { MTKView() }\n"
    + COORDINATOR +
    "}\n#endif\n")


def test_the_duplicated_coordinator_is_found_with_both_locations():
    (length, starts, first), = duplicated_regions(RUN47)
    # The closing `}` of each struct is identical too, so the region may run one line past the class.
    assert length >= COORDINATOR.count("\n")
    assert len(starts) == 2
    assert first == "final class Coordinator {"
    assert RUN47.split("\n")[starts[0] - 1].strip() == first


def test_the_note_names_the_branches_so_widening_is_known_to_be_useless():
    note = describe_duplicated_regions(RUN47)
    assert "appear 2x" in note
    assert "`#if os(macOS)`" in note and "`#elseif os(iOS) || os(visionOS)`" in note
    assert "makeNSView" in note


def test_a_file_without_a_repeated_region_has_no_note():
    unique = "\n".join(f"let value{i} = compute(argument: {i}, scale: {i * 3}, label: \"row {i}\")"
                       for i in range(40))
    assert describe_duplicated_regions(unique) == ""


def test_a_table_of_identical_rows_is_not_a_duplicated_region():
    rows = "    entry(kind: .wall, damage: 0, poisoned: false, sprite: \"wall_tile_a\")\n" * 30
    assert duplicated_regions(rows) == []


def test_scaffolding_alone_is_not_reported():
    braces = ("}\n" * 8 + "\n" * 4) * 3
    assert duplicated_regions(braces) == []


def test_the_note_rides_under_the_file_only_in_edit_mode():
    files = [("ElectricSheep/MetalViewRepresentable.swift", RUN47)]
    edit = messages._render_existing_files(files, edit_mode=True)
    whole = messages._render_existing_files(files, edit_mode=False)
    assert "Repeated text in `ElectricSheep/MetalViewRepresentable.swift`" in edit
    assert "Repeated text" not in whole
    assert edit.index("Repeated text") > edit.index("````\n\n")   # after the file body
