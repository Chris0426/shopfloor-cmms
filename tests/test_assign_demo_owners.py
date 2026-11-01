"""scripts/assign_demo_owners.py:純函式層,不需 DB。"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "assign_demo_owners.py"
_spec = importlib.util.spec_from_file_location("assign_demo_owners_script", _SCRIPT)
owners_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = owners_mod
_spec.loader.exec_module(owners_mod)


def test_story_asset_has_owner():
    # EID-97061(影片故事的 UV Oven #1)必須在涵蓋範圍內且有負責人
    assert owners_mod.ASSET_COUNT >= 61
    assert owners_mod.owners_for(60) == ["Jordan Lee"]


def test_every_asset_owned_by_known_engineer():
    for i in range(owners_mod.ASSET_COUNT):
        owners = owners_mod.owners_for(i)
        assert owners and set(owners) <= set(owners_mod.OWNERS)
