"""Tests that the integration version stays consistent across files."""

import json
from pathlib import Path

from custom_components.climate_advisor.const import VERSION

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "manifest.json"


class TestVersionSync:
    """VERSION in const.py must match manifest.json."""

    def test_version_matches_manifest(self):
        manifest = json.loads(MANIFEST_PATH.read_text())
        assert manifest["version"] == VERSION, (
            f"const.py VERSION ({VERSION}) != manifest.json version "
            f"({manifest['version']}). Update both when releasing."
        )

    def test_version_is_semver(self):
        """VERSION must be a valid semver string (MAJOR.MINOR.PATCH)."""
        parts = VERSION.split(".")
        assert len(parts) == 3, f"VERSION '{VERSION}' is not MAJOR.MINOR.PATCH"
        for part in parts:
            assert part.isdigit(), f"VERSION segment '{part}' is not numeric"


CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def _changelog_versions() -> list[str]:
    import re

    return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", CHANGELOG_PATH.read_text(encoding="utf-8"), re.M)


class TestChangelogVersions:
    """The changelog must describe each release once, newest first, starting at VERSION.

    Several PRs that merge together each bumping to the same next version left two 0.9.41 headings and an out-of-order
    0.9.42 that was never released (fixed in 0.9.44).
    """

    def test_newest_entry_is_current_version(self):
        assert _changelog_versions()[0] == VERSION, (
            f"CHANGELOG.md's newest entry is {_changelog_versions()[0]}, but VERSION is {VERSION}. "
            "Add this PR's entry at the top under the version it bumps to."
        )

    def test_no_version_repeats(self):
        versions = _changelog_versions()
        repeated = sorted({v for v in versions if versions.count(v) > 1})
        assert not repeated, f"CHANGELOG.md lists these versions more than once: {repeated}. Renumber after a merge."

    def test_versions_strictly_descending(self):
        versions = _changelog_versions()
        keys = [tuple(int(p) for p in v.split(".")) for v in versions]
        bad = [(versions[i], versions[i + 1]) for i in range(len(keys) - 1) if keys[i] <= keys[i + 1]]
        assert not bad, f"CHANGELOG.md versions out of order (newer must come first): {bad}"
