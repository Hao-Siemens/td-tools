"""Generate WoT Thing Descriptions for every supported reference device schema.

Walks the ``device-payload-schema`` submodule, converts each device schema that
falls within the binding's supported subset into a Thing Description under
``examples/devices/<vendor>/<model>.td.json``, and prints a coverage report
listing what was generated and what was skipped (and why).

Usage::

    uv run --no-sync python -m scripts.generate_device_tds
"""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any

import yaml

from lorawan_wot.schema_to_td import UnsupportedSchemaError, payload_schema_to_td

REPO_ROOT = Path(__file__).resolve().parents[1]
DEVICES_DIR = REPO_ROOT / "external" / "device-payload-schema" / "schemas" / "devices"
OUTPUT_DIR = REPO_ROOT / "examples" / "devices"


def _expected_output_path(rel_schema_path: str) -> Path:
    """Return the generated TD path for a catalog-relative source schema path."""
    return OUTPUT_DIR / Path(rel_schema_path).with_suffix(".td.json")


def _prune_stale_outputs(current_rel_paths: set[str]) -> list[Path]:
    """Delete generated TDs that no longer correspond to a supported schema."""
    expected = {_expected_output_path(rel).resolve() for rel in current_rel_paths}
    removed: list[Path] = []
    for existing in OUTPUT_DIR.rglob("*.td.json"):
        if existing.resolve() in expected:
            continue
        existing.unlink()
        removed.append(existing)

    for directory in sorted(OUTPUT_DIR.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    return removed


def _skip_bucket(exc: UnsupportedSchemaError) -> str:
    """Return the coverage-report label for a skip.

    The label comes straight from the exception's structured
    :class:`~lorawan_wot.schema_to_td.SkipReason`, so the report stays correct
    even if the human-readable error message is reworded.
    """
    return exc.reason.value


def convert_catalog() -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], int]:
    """Convert every reference device schema without writing anything to disk.

    Returns ``(tds, skipped, scanned)``: ``tds`` maps each device schema's
    catalog-relative POSIX path to its Thing Description, ``skipped`` buckets the
    unconvertible schemas by :class:`~lorawan_wot.schema_to_td.SkipReason` label,
    and ``scanned`` counts the schema files examined.

    Kept separate from :func:`generate` so callers that only need the conversion
    result -- the golden-snapshot builder in ``tests/snapshot.py``, for instance --
    share this walk instead of reimplementing it and drifting from it.
    """
    schema_paths = sorted(DEVICES_DIR.rglob("*.yaml"))
    tds: dict[str, dict[str, Any]] = {}
    skipped: dict[str, list[str]] = collections.defaultdict(list)

    for schema_path in schema_paths:
        rel = schema_path.relative_to(DEVICES_DIR).as_posix()
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        try:
            tds[rel] = payload_schema_to_td(schema, source=schema_path.name)
        except UnsupportedSchemaError as exc:
            skipped[_skip_bucket(exc)].append(rel)

    return tds, skipped, len(schema_paths)


def generate() -> int:
    """Generate all supported device TDs; return the number written."""
    tds, skipped, scanned = convert_catalog()
    if not tds:
        # Pruning below would delete the whole catalog, and examples/devices/ is
        # gitignored, so those files cannot be restored with git. Converting
        # nothing at all is never a legitimate outcome: it means the submodule is
        # missing or the converter is broken. Fail loudly instead of emptying the
        # directory and reporting success.
        raise SystemExit(
            f"no device schema converted ({scanned} scanned under {DEVICES_DIR}); "
            "refusing to prune the generated catalog, which is not recoverable "
            "from git. Run `git submodule update --init --recursive` and check the "
            "converter before regenerating."
        )

    generated: list[Path] = []
    removed = _prune_stale_outputs(set(tds))

    for rel, td in tds.items():
        out_path = _expected_output_path(rel)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(td, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        generated.append(out_path)

    _report(scanned, generated, skipped, removed)
    return len(generated)


def _report(
    total: int, generated: list[Path], skipped: dict[str, list[str]], removed: list[Path]
) -> None:
    """Print a human-readable coverage summary."""
    print(f"Device schemas scanned: {total}")
    print(f"Thing Descriptions generated: {len(generated)}")
    print(f"  written under: {OUTPUT_DIR.relative_to(REPO_ROOT)}")
    print(f"Stale generated TDs removed: {len(removed)}")
    skipped_total = sum(len(v) for v in skipped.values())
    print(f"Skipped (unsupported subset): {skipped_total}")
    for reason in sorted(skipped, key=lambda r: -len(skipped[r])):
        print(f"  {len(skipped[reason]):4d}  {reason}")


if __name__ == "__main__":
    generate()
