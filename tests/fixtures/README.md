# Test fixtures

Artefacts frozen byte for byte from real pipeline runs, so a test about a run
keeps meaning the same thing after the run's workspace moves on. Load them with
`load_fixture(name)` from `tests/fixture_files.py`.

Spec ids are the ones the tests' docstrings record; where none is named, the
row says so. The run numbers match the runs table in the top-level README.

| File | From | What it is | Used by |
| --- | --- | --- | --- |
| `run7_design_v3.md` | Run 7, Centipede (spec_9e190582); rejected by hand at gate_8e14f676 | The architect's third design revision for slice 1. It declares no `MushroomField` although a file is typed with it, and holds a tuple collection on an Equatable type | `test_design_completeness.py` (DEV-509), `test_tuple_conformance.py` (DEV-525) |
| `dev809_run60_design.md` | Run 60, Electric Sheep (DEV-814, the Intensity slider); spec not named | The design, correct from round 0, that drew four false testability findings | `test_testability_false_positives.py` (DEV-809) |
| `dev822_run61_design.md` | Run 61, Electric Sheep (spec_360d8d96, DEV-815, the Metrics panel) | The final design.md. `undeclared_mutability` misread enums nested in a class, and `prose_seam` fired, correctly, on "continues from C5a" setups | `test_testability_run61.py` (DEV-822) |
| `dev828_run64_design.md` | Run 64, Centipede (spec_28e195d8, DEV-826, the HUD) | The final design.md, which adds `hud` to `FrameSnapshot` | `test_mutability_attribution.py` (DEV-828) |
| `dev828_FrameSnapshot.swift` | Run 64, Centipede `e09322a` | The `FrameSnapshot.swift` served to the run 64 architect; it holds three value types | `test_mutability_attribution.py` (DEV-828) |
| `dev831_run66_design.md` | Run 66, LLab (spec_ae73361b, the first LLab run) | The attempt-1 design, which adds `axiomError` to `class LSystem` | `test_mutability_swift_only.py` (DEV-831) |
| `dev831_run66_round2_design.md` | Run 66, LLab (spec_ae73361b) | The round 2 design, which lists header-resident C++ types in Data Models | `test_mutability_swift_only.py` (DEV-831) |
| `dev831_LSystem.h` | Run 66, LLab (spec_ae73361b) | The C++ header `LLab Shared/Generator/LSystem.h` served to the architect | `test_mutability_swift_only.py` (DEV-831) |
| `dev833_run68_round1_design.md` | Run 68, LLab (spec_9decc8db, DEV-832, USD parser validation) | The round 1 design, whose seam C7 compares an `NSError` domain with a string literal that is also a type's name | `test_equatable_string_literals.py` (DEV-833) |
| `dev838_swift_compile_failure.txt` | Centipede (spec_1ba2db3d), `retry_history/retry_5/build_check_output.txt`, whole file | A `swift test` build that failed on two `'let' constant` mutability errors, with one style warning and the bare `error: fatalError` trailer | `test_diagnostics.py` (DEV-838) |
| `dev838_xcodebuild_test_failure.txt` | Electric Sheep (spec_74c42cc8), `test_output.txt`, the last 29 lines of 6,120 | An `xcodebuild test` run: eight `Test case '…' passed/failed on` lines, two failed, and `** TEST FAILED **` | `test_diagnostics.py` (DEV-838) |
| `dev838_pytest_pass.txt` | Self-target (spec_823fee30), `test_output.txt`, whole file | A pytest run of four tests, all passed | `test_diagnostics.py` (DEV-838) |
| `dev838_node_test_harness_failure.txt` | Centipede JS (spec_54b2c1b3), `retry_history/retry_4/test_output.txt`, lines 1–38 and the last 9 lines | A node:test run whose test files failed to import (`does not provide an export named`), then the TAP footer: 71 tests, 31 pass | `test_diagnostics.py` (DEV-838) |
| `dev838_swift_test_crash.txt` | Run 9 of DEV-102, Centipede (spec_9ff962b9), `test_output.txt`, whole file | `Build complete!`, then the swift-testing helper exited on signal 5 before any test reported; carries the blocking `[#no-usage]` warning | `test_diagnostics.py` (DEV-838) |

`tests/seams/fixtures/` is a separate set, owned by the seam harness
(`tests/seams/seam_harness.py`).
