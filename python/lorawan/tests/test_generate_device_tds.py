"""Guard the catalog generator's on-disk behaviour.

``examples/devices/`` is generated and gitignored, so anything the generator
deletes cannot be restored with git. These tests pin the two rules that keep that
safe: stale outputs are pruned, and a run that converts nothing prunes nothing.
"""

from __future__ import annotations

import json

import pytest

from scripts import generate_device_tds


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Point the generator at a throwaway output tree."""
    output_dir = tmp_path / "examples" / "devices"
    output_dir.mkdir(parents=True)
    monkeypatch.setattr(generate_device_tds, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(generate_device_tds, "REPO_ROOT", tmp_path)
    return output_dir


def test_generate_prunes_stale_catalog_files(catalog, monkeypatch):
    """A TD whose schema no longer converts is removed; current output survives."""
    active = catalog / "vendor" / "active.td.json"
    stale = catalog / "vendor" / "stale.td.json"
    keep = catalog / ".gitkeep"
    notes = catalog / "vendor" / "notes.txt"

    active.parent.mkdir(parents=True)
    # Pre-created so the run exercises prune-then-rewrite, not just a fresh write.
    active.write_text('{"title": "previous"}\n', encoding="utf-8")
    stale.write_text("{}\n", encoding="utf-8")
    keep.write_text("", encoding="utf-8")
    notes.write_text("scratch", encoding="utf-8")

    monkeypatch.setattr(
        generate_device_tds,
        "convert_catalog",
        lambda: ({"vendor/active.yaml": {"title": "active"}}, {}, 1),
    )

    written = generate_device_tds.generate()

    assert written == 1
    assert json.loads(active.read_text(encoding="utf-8")) == {"title": "active"}
    assert not stale.exists()
    # Only generated TDs are pruned: tracked and unrelated files are untouched.
    assert keep.exists()
    assert notes.exists()


def test_generate_refuses_to_prune_when_nothing_converts(catalog, monkeypatch):
    """An empty conversion must not empty the catalog.

    A run that converts nothing means the schema submodule is missing or the
    converter is broken. Pruning on that result would delete every generated TD,
    and the directory is gitignored, so the files would be unrecoverable.
    """
    existing = catalog / "vendor" / "device.td.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(generate_device_tds, "convert_catalog", lambda: ({}, {}, 0))

    with pytest.raises(SystemExit, match="refusing to prune"):
        generate_device_tds.generate()

    assert existing.exists()


def test_prune_stale_outputs_keeps_current_files(catalog):
    """A TD still produced by the current run is left alone."""
    current = catalog / "vendor" / "device.td.json"
    current.parent.mkdir(parents=True)
    current.write_text("{}\n", encoding="utf-8")

    removed = generate_device_tds._prune_stale_outputs({"vendor/device.yaml"})

    assert removed == []
    assert current.exists()


def test_prune_stale_outputs_tolerates_missing_output_dir(tmp_path, monkeypatch):
    """Pruning a catalog that was never generated is a no-op, not an error."""
    monkeypatch.setattr(generate_device_tds, "OUTPUT_DIR", tmp_path / "absent")

    assert generate_device_tds._prune_stale_outputs({"vendor/device.yaml"}) == []
