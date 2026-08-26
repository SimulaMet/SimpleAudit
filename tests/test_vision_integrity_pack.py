"""Tests for the vision_integrity scenario pack.

This pack is the only one that ships binary assets, so the usual scenario-data
checks are not enough: a `file_uri` that no longer resolves, or an asset that
is not really an image, would only surface as an ERROR result mid-audit after
the user has paid for the scenarios before it.

These tests double as the corpus check for the `file_uri` validation work in
issue #40 — if content sniffing or a format allowlist ever rejects a shipped
asset, this file fails rather than the feature silently breaking for anyone
running the pack.
"""

import pathlib

import pytest

from simpleaudit.scenarios import SCENARIO_PACKS, get_scenarios
from simpleaudit.scenarios.vision_integrity import IMAGES
from simpleaudit.utils import image_content_block, image_media_type

PACK = "vision_integrity"

#: Magic bytes for the formats the vision APIs accept. Kept local to the test
#: on purpose: it should fail if the shipped assets drift, not track whatever
#: utils happens to allow at the time.
_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
}


def _file_uris(scenario):
    uris = scenario.get("file_uri")
    if not uris:
        return []
    return [uris] if isinstance(uris, str) else list(uris)


def _all_uris():
    return [uri for s in get_scenarios(PACK) for uri in _file_uris(s)]


class TestPackRegistration:
    def test_pack_is_registered(self):
        assert PACK in SCENARIO_PACKS

    def test_pack_is_not_in_all(self):
        """Attachments reach target, judge and auditor, so this pack needs three
        vision-capable models — 'all' cannot assume that."""
        all_names = {s["name"] for s in get_scenarios("all")}
        pack_names = {s["name"] for s in get_scenarios(PACK)}
        assert not (all_names & pack_names)

    def test_every_scenario_attaches_a_file(self):
        for s in get_scenarios(PACK):
            assert _file_uris(s), f"{s['name']}: no file_uri — it would not test vision"

    def test_control_scenario_is_first(self):
        """A failing control means the model never saw the image, which makes the
        remaining results meaningless. It has to be the first thing read."""
        assert "Control" in get_scenarios(PACK)[0]["name"]

    def test_exactly_one_scenario_omits_test_prompt(self):
        """The auditor-composed scenario is deliberately prompt-less; the rest
        must send verbatim so the stimulus is fixed."""
        without = [s["name"] for s in get_scenarios(PACK) if not s.get("test_prompt")]
        assert without == ["Vision - Auditor-Composed Opening Probe"]


class TestShippedAssets:
    def test_every_file_uri_resolves(self):
        for uri in _all_uris():
            assert pathlib.Path(uri).is_file(), f"missing asset: {uri}"

    def test_paths_are_absolute(self):
        """Relative paths resolve against the process CWD, so a packaged pack
        must not use them."""
        for uri in _all_uris():
            assert pathlib.Path(uri).is_absolute(), f"not absolute: {uri}"

    def test_every_asset_is_really_an_image(self):
        for uri in _all_uris():
            header = pathlib.Path(uri).read_bytes()[:8]
            assert any(header.startswith(sig) for sig in _SIGNATURES), (
                f"{pathlib.Path(uri).name} does not start with a known image "
                f"signature (got {header!r})"
            )

    def test_declared_media_type_matches_the_bytes(self):
        for uri in _all_uris():
            declared = image_media_type(uri)
            header = pathlib.Path(uri).read_bytes()[:8]
            actual = next(mt for sig, mt in _SIGNATURES.items() if header.startswith(sig))
            assert declared == actual, f"{uri}: named {declared}, bytes say {actual}"

    def test_assets_stay_small_enough_to_send(self):
        """Base64 inflates by 4/3 and providers cap per-image payloads. These are
        simple charts; anything approaching the cap means one got replaced."""
        for uri in _all_uris():
            size = pathlib.Path(uri).stat().st_size
            assert size < 1_000_000, f"{uri} is {size:,} bytes — unexpectedly large"

    def test_no_orphaned_assets(self):
        """Every shipped PNG should be referenced by a scenario, or it is dead
        weight in the wheel."""
        referenced = {pathlib.Path(uri).name for uri in _all_uris()}
        shipped = {p.name for p in IMAGES.glob("*.png")}
        assert shipped == referenced, f"unreferenced: {sorted(shipped - referenced)}"


class TestAssetsAreUsable:
    @pytest.mark.parametrize("name", sorted(p.name for p in IMAGES.glob("*.png")))
    def test_asset_encodes_to_a_content_block(self, name):
        block = image_content_block(str(IMAGES / name))
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/png;base64,")

    def test_the_multi_image_scenario_uses_distinct_files(self):
        multi = [s for s in get_scenarios(PACK) if isinstance(s.get("file_uri"), list)]
        assert multi, "expected at least one list-valued file_uri in this pack"
        for s in multi:
            uris = s["file_uri"]
            assert len(set(uris)) == len(uris), f"{s['name']}: duplicate file_uri entries"
