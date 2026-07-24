"""Packaging: the wheel must ship the built Control Shell (dist is gitignored)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

hatchling = pytest.importorskip("hatchling")

ROOT = Path(__file__).resolve().parents[1]
DIST_INDEX = ROOT / "qbx" / "web" / "matcher" / "dist" / "index.html"


@pytest.mark.skipif(not DIST_INDEX.is_file(), reason="Control Shell not built in this checkout")
def test_wheel_contains_built_control_shell(tmp_path):
    from hatchling.builders.wheel import WheelBuilder

    builder = WheelBuilder(str(ROOT))
    artifacts = list(builder.build(directory=str(tmp_path)))
    assert artifacts, "hatchling produced no wheel"

    names = zipfile.ZipFile(artifacts[0]).namelist()
    assert "qbx/web/matcher/dist/index.html" in names
    assert any(n.startswith("qbx/web/matcher/dist/assets/") and n.endswith(".js") for n in names)
    # Source of the SPA is not needed at runtime; node_modules must never ship.
    assert not any("node_modules" in n for n in names)
