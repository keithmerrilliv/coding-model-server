"""DEV-782: the whole-file template's placeholder line is never written to disk."""
from coding_model_autonomous import executor as ex

RUN50_SHAPE = """Fixing the four diagnostics.

<<<FILE: ElectricSheep/AudioManager.swift>>>
<complete file content>
import AVFoundation
import Foundation

@Observable
final class AudioManager {
    var isPlaying = false
}
<<<END_FILE>>>
"""


def test_run50_shape_drops_the_placeholder_and_keeps_the_file():
    r = ex.parse_implementer_response(RUN50_SHAPE)
    assert isinstance(r, ex.ImplementerResult)
    files = dict(r.files)
    body = files["ElectricSheep/AudioManager.swift"]
    assert body.startswith("import AVFoundation\n")
    assert "<complete file content>" not in body
    assert r.echoed_placeholders == []


def test_long_placeholder_form_and_trailing_placeholder_are_stripped():
    text = ("<<<FILE: a.py>>>\n<complete file content — NOT a diff, the ENTIRE file>\n"
            "x = 1\n<complete file content>\n<<<END_FILE>>>\n")
    r = ex.parse_implementer_response(text)
    assert dict(r.files)["a.py"].strip() == "x = 1"


def test_a_block_that_is_only_the_placeholder_is_an_echoed_template():
    text = "<<<FILE: another/file.py>>>\n<complete file content>\n<<<END_FILE>>>\n<<<FILE: b.py>>>\ny = 2\n<<<END_FILE>>>\n"
    r = ex.parse_implementer_response(text)
    assert r.echoed_placeholders == ["another/file.py"]
    assert [p for p, _ in r.files] == ["b.py"]


def test_a_file_that_legitimately_starts_with_an_angle_bracket_is_untouched():
    xml = '<?xml version="1.0"?>\n<plist version="1.0"><dict/></plist>\n'
    text = f"<<<FILE: Info.plist>>>\n{xml}<<<END_FILE>>>\n"
    r = ex.parse_implementer_response(text)
    assert dict(r.files)["Info.plist"].strip() == xml.strip()
    # and a placeholder-looking line in the MIDDLE of a file is the model's own content
    mid = "<<<FILE: c.py>>>\nx = 1\n<complete file content>\ny = 2\n<<<END_FILE>>>\n"
    assert "<complete file content>" in dict(ex.parse_implementer_response(mid).files)["c.py"]
