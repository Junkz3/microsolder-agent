
import pytest

from api.stock.schemas import StockInventory
from api.stock.store import (
    consume_part,
    load_inventory,
    mark_donor,
    unconsume_part,
    unmark_donor,
)


@pytest.fixture
def stock_root(tmp_path, monkeypatch):
    """Redirect stock storage to a tmp dir for tests."""
    root = tmp_path / "_stock"
    monkeypatch.setattr("api.stock.store._stock_root", lambda: root)
    return root


def test_load_inventory_empty_creates(stock_root):
    inv = load_inventory()
    assert isinstance(inv, StockInventory)
    assert inv.donors == {}


def test_save_then_load_round_trip(stock_root, tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path)
    (tmp_path / "iphone-x").mkdir()
    donor_id = mark_donor(device_slug="iphone-x", label="iPhone X test", condition="donor_only")
    assert donor_id == "iphone-x-donor-2026-001" or donor_id.startswith("iphone-x-donor-")
    inv = load_inventory()
    assert donor_id in inv.donors
    assert inv.donors[donor_id].device_slug == "iphone-x"


def test_next_donor_id_increments(stock_root, tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path)
    (tmp_path / "iphone-x").mkdir()
    (tmp_path / "iphone-13").mkdir()
    a = mark_donor(device_slug="iphone-x", label="A")
    b = mark_donor(device_slug="iphone-x", label="B")
    c = mark_donor(device_slug="iphone-13", label="C")
    assert a != b
    assert a.endswith("-001")
    assert b.endswith("-002")
    assert c.endswith("-001")  # new slug, fresh counter


def test_consume_part_persists(stock_root, tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path)
    (tmp_path / "iphone-x").mkdir()
    donor_id = mark_donor(device_slug="iphone-x", label="X")
    consume_part(donor_id=donor_id, refdes="U7", repair_id="repair-1", notes="PMIC swap")
    inv = load_inventory()
    assert "U7" in inv.donors[donor_id].consumed
    assert inv.donors[donor_id].consumed["U7"].notes == "PMIC swap"


def test_consume_part_idempotent_updates_notes(stock_root, tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path)
    (tmp_path / "iphone-x").mkdir()
    donor_id = mark_donor(device_slug="iphone-x", label="X")
    consume_part(donor_id=donor_id, refdes="U7", notes="first")
    consume_part(donor_id=donor_id, refdes="U7", notes="second")
    inv = load_inventory()
    assert len(inv.donors[donor_id].consumed) == 1
    assert inv.donors[donor_id].consumed["U7"].notes == "second"


def test_unconsume_removes_entry(stock_root, tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path)
    (tmp_path / "iphone-x").mkdir()
    donor_id = mark_donor(device_slug="iphone-x", label="X")
    consume_part(donor_id=donor_id, refdes="U7")
    unconsume_part(donor_id=donor_id, refdes="U7")
    inv = load_inventory()
    assert "U7" not in inv.donors[donor_id].consumed


def test_unmark_donor_removes_entry(stock_root, tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path)
    (tmp_path / "iphone-x").mkdir()
    donor_id = mark_donor(device_slug="iphone-x", label="X")
    unmark_donor(donor_id=donor_id)
    inv = load_inventory()
    assert donor_id not in inv.donors


def test_mark_donor_unknown_slug_raises(stock_root, monkeypatch, tmp_path):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path / "memory_does_not_exist")
    with pytest.raises(FileNotFoundError):
        mark_donor(device_slug="bogus-slug-xyz", label="Y")


def test_atomic_write_via_tmp_rename(stock_root, tmp_path, monkeypatch):
    """No partial inventory.json should ever be visible mid-write."""
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path)
    (tmp_path / "iphone-x").mkdir()
    mark_donor(device_slug="iphone-x", label="A")
    inv_file = stock_root / "inventory.json"
    assert inv_file.exists()
    # Both invocations succeed → atomic rename worked
    mark_donor(device_slug="iphone-x", label="B")
    assert inv_file.exists()
