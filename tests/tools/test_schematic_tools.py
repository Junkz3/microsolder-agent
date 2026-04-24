from pathlib import Path
import json
from api.session.state import SessionState
from api.tools.schematic import mb_schematic_graph


def test_schematic_graph_cache_hits(tmp_path: Path, monkeypatch):
    slug = "demo"
    pack = tmp_path / slug
    pack.mkdir()
    graph = {"power_rails": {}, "boot_sequence": [], "components": []}
    (pack / "electrical_graph.json").write_text(json.dumps(graph))

    session = SessionState()
    reads: list[Path] = []
    orig = Path.read_text
    def counting(self, *args, **kwargs):
        if self.name == "electrical_graph.json":
            reads.append(self)
        return orig(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counting)

    mb_schematic_graph(device_slug=slug, memory_root=tmp_path, query="list_rails", session=session)
    mb_schematic_graph(device_slug=slug, memory_root=tmp_path, query="list_rails", session=session)

    assert len(reads) == 1, f"expected 1 disk read, got {len(reads)}"
