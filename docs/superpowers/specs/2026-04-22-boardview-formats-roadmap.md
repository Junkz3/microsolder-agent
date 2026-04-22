# Boardview formats — roadmap

microsolder-agent is designed to read any PCB boardview format a technician might legitimately have. The parser architecture (`api/board/parser/`) dispatches via file extension + content-sniffing to a format-specific parser that populates the unified `api/board/model.py::Board` model. Adding a new format = one new file in `api/board/parser/`, registered automatically via the `@register` decorator. No changes to `base.py`, the validator, the agent, or the UI.

This document tracks the status of every format we know about.

## Fixture policy

Per CLAUDE.md hard rule #4 (**open hardware only**), we commit fixtures under `board_assets/` **only** for genuinely open-source hardware (MNT Reform, whitequark example, our synthetic bilayer). We do **not** commit proprietary boardviews (Apple, Samsung, ASUS, Lenovo, ZXW, WUXINJI, etc.). Users who have legitimately-acquired proprietary files upload them through the UI dropzone at runtime — their responsibility, not ours. The parser code itself is format-agnostic and may be distributed freely (precedent: OpenBoardView is open source and reads proprietary formats).

## Status key

- **DONE** — parser implemented, tested, wired into registry
- **STUB** — placeholder file exists, declares extension, raises `NotImplementedError` on `parse()`
- **FUTURE** — not yet stubbed

## Format matrix

| Extension | Format | Origin / vendor | Our parser | Status | Notes |
|-----------|--------|-----------------|------------|--------|-------|
| `.brd` | Test_Link | Landrex (80s) | `test_link.py::BRDParser` | **DONE** | Refuses OBV-signature obfuscated files. Content-sniffed via `str_length:` marker. |
| `.brd` | BRD2 | whitequark/kicad-boardview | `brd2.py::BRD2Parser` | **DONE** | Content-sniffed via `BRDOUT:` marker. 0BSD reference fixture at `web/boards/whitequark-example.brd`. |
| `.kicad_pcb` | KiCad native | KiCad project | `kicad.py::KicadPcbParser` | **DONE** | Rich source — value, footprint, rotation, pad shape / size. Via `pcbnew` Python API. |
| `.fz` | PCB Repair Tool | community reverse-eng | `fz.py::FZParser` | **STUB** | Binary format. OpenBoardView-compat. |
| `.bdv` | HONHAN BoardViewer | HONHAN (CN) | `bdv.py::BDVParser` | **STUB** | Repair shop format. |
| `.asc` | ASUS TSICT | ASUS | `asc.py::ASCParser` | **STUB** | ASUS internal test viewer export. |
| `.bv` | ATE Boardview | ATE | `bv.py::BVParser` | **STUB** | Version 1.5.0. Drag-and-drop only. |
| `.gr` | BoardView R5.0 | generic | `gr.py::GRParser` | **STUB** |  |
| `.cst` | Card Analysis ST | IBM/Lenovo | `cst.py::CSTParser` | **STUB** | Tool: Castw IBM v3.32. |
| `.tvw` | Tebo IctView | Tebo | `tvw.py::TVWParser` | **STUB** | Versions 3.0, 4.0. |
| `.f2b` | Unisoft ProntoPLACE | Unisoft | `f2b.py::F2BParser` | **STUB** | Place5 converter. |
| `.cad` | Generic CAD | BoardViewer 2.1.0.8 | `cad.py::CADParser` | **STUB** | Umbrella format. |

## Unified model

All parsers populate the same `Board` object. Each format fills what it can; absent fields stay `None`. The frontend and agent degrade gracefully — a part with `value == None` renders as its `refdes` only, a part with `value == "10µF"` renders as `refdes + value`.

Required fields (every parser must fill these):
- `refdes`, `bbox`, `layer`, `pin_refs`
- `pin.pos`, `pin.net`, `pin.layer`, `pin.part_refdes`, `pin.index`

Optional enrichments (only richer formats — `.kicad_pcb` is the current gold standard):
- `part.value`, `part.footprint`, `part.rotation_deg`
- `pin.pad_shape`, `pin.pad_size`

## When to promote a STUB to DONE

1. A concrete user need arises (request, demo, repair scenario).
2. A legitimate open test fixture is available (ideally community-distributed, not leaked).
3. The format has public documentation or is reverse-engineered elsewhere under a permissive license (reference: OpenBoardView source).

Until then the stub file exists so that:
- the registry is already wired (a user uploading `.fz` gets a clean `501 Not Implemented`, not a confusing `415 Unsupported Format`)
- the scope is visibly tracked (anyone scanning `api/board/parser/` sees the roadmap at a glance)
- a future implementer has a drop-in location without touching `base.py`

## References

- OpenBoardView source (multi-format reader, MIT): https://github.com/OpenBoardView/OpenBoardView
- whitequark/kicad-boardview (0BSD, KiCad→BRD2/BVRAW): https://github.com/whitequark/kicad-boardview
- KiCad `.kicad_pcb` format spec: https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/
- Format directory (catalog of boardview extensions): https://gist.github.com/vyach-vasiliev/35d610e14c40b4060f5d929ac70746a3
