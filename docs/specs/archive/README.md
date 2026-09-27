# Archived specs

These specs are kept for history and are not submittable. Each one was stale on
2026-09-26 (DEV-829): its work reached the target repo's `main` another way, or
its ticket is Done. `docs/specs/` holds only live work.

| Spec | Target | Why archived |
| --- | --- | --- |
| `electric_sheep_frame_phase_and_foveation.md` | electric-sheep | DEV-589/591: frame lifecycle fixed by run 58; rasterization maps bound on main |
| `electric_sheep_audio_strike_race.md` | electric-sheep | DEV-594/200/201: hand-merged as ES `ab3e7ec` |
| `electric_sheep_dtype_and_2d_logits.md` | electric-sheep | DEV-585/587 Done |
| `electric_sheep_triple_buffering.md` | electric-sheep | DEV-590: hand-delivered as ES `6506baf` after run 47 |
| `centipede_metal4_slice2.md` … `slice5.md` | centipede | The August Metal 4 plan was abandoned; the CentipedeRender slices (runs 49–65) replaced it |
| `dev602a_write_guards.md`, `dev602b_tested_manifest.md`, `dev602c_delivery_verify.md` | self | DEV-602 Done (guards landed through run 20) |
| `dev602_reviewer_overwrite_containment.md` | self | DEV-602 Done |
| `dev641_trailing_newline.md` | self | DEV-641 Done |
| `dev643_design_file_count.md` | self | DEV-643 Done |
| `dogfood_dev420.md` | self | DEV-420 Done |

To revive one, move it back with `git mv` and re-verify every quoted anchor
against the target's current `main` first. Anchors in these files were written
against commits that have since moved.
