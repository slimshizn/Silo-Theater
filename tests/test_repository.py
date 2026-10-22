"""Validate the static Kodi repository as an installable, immutable release."""

import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from scripts.build import production_files
from scripts.build_repository import PAGES_BASE, REPOSITORY_ID, build_repository, validate_repository


ROOT = Path(__file__).resolve().parents[1]


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        for path in production_files(ROOT):
            target = self.root / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        shutil.copytree(ROOT / "repository", self.root / "repository")
        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_text('<!doctype html><a href="repository.silo.theater-1.0.0.zip">Install</a>\n')
        self.output = Path(self.temporary.name) / "pages"

    def test_catalog_contains_both_addons_and_matches_the_exact_md5(self):
        paths = build_repository(self.root, self.output)
        contents = paths["index"].read_bytes()
        catalog = ET.fromstring(contents)
        self.assertEqual(catalog.tag, "addons")
        self.assertEqual([(item.get("id"), item.get("version")) for item in catalog],
                         [(REPOSITORY_ID, "1.0.0"), ("script.silo", "0.1.0")])
        expected = hashlib.md5(contents, usedforsecurity=False).hexdigest()
        self.assertEqual(paths["checksum"].read_text(), expected + "\n")
        for path in (paths["installer"], paths["addon_zip"], paths["repository_zip"]):
            self.assertEqual(path.with_suffix(".zip.sha256").read_text(),
                             hashlib.sha256(path.read_bytes()).hexdigest() + "\n")

    def test_installer_and_downloads_have_the_kodi_layout(self):
        paths = build_repository(self.root, self.output)
        self.assertEqual(paths["installer"].name, "repository.silo.theater-1.0.0.zip")
        self.assertEqual(paths["repository_zip"].relative_to(self.output).as_posix(),
                         "zips/repository.silo.theater/repository.silo.theater-1.0.0.zip")
        self.assertEqual(paths["addon_zip"].relative_to(self.output).as_posix(),
                         "zips/script.silo/script.silo-0.1.0.zip")
        self.assertEqual(paths["installer"].read_bytes(), paths["repository_zip"].read_bytes())
        with zipfile.ZipFile(paths["installer"]) as archive:
            self.assertEqual(archive.namelist(), [REPOSITORY_ID + "/LICENSE", REPOSITORY_ID + "/addon.xml"])
            self.assertEqual(archive.read(REPOSITORY_ID + "/LICENSE"), (self.root / "LICENSE").read_bytes())
            manifest = ET.fromstring(archive.read(REPOSITORY_ID + "/addon.xml"))
            directory = manifest.find("./extension[@point='xbmc.addon.repository']/dir")
            self.assertIsNotNone(directory)
            self.assertEqual(directory.findtext("info"), PAGES_BASE + "addons.xml")
            self.assertEqual(directory.find("checksum").get("verify"), "md5")
            self.assertEqual(directory.find("datadir").get("zip"), "true")
            self.assertEqual(directory.findtext("hashes"), "sha256")
            self.assertIsNone(archive.testzip())
        with zipfile.ZipFile(paths["addon_zip"]) as archive:
            self.assertIn("script.silo/default.py", archive.namelist())
            self.assertTrue(all(name.startswith("script.silo/") for name in archive.namelist()))
            self.assertFalse(any("repository/" in name or "site/" in name or "tests/" in name or "docs/" in name for name in archive.namelist()))
        self.assertEqual(paths["landing_page"].read_bytes(), (self.root / "site/index.html").read_bytes())
        self.assertEqual((self.output / ".nojekyll").read_bytes(), b"")

    def test_two_builds_are_identical_and_preserve_older_packages(self):
        build_repository(self.root, self.output)
        old = self.output / "zips/script.silo/script.silo-0.0.9.zip"
        old.write_bytes(b"older published package")
        snapshot = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        build_repository(self.root, self.output)
        rebuilt = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(snapshot, rebuilt)

    def test_changed_bytes_at_the_same_version_fail_without_updating_the_site(self):
        build_repository(self.root, self.output)
        snapshot = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        with (self.root / "default.py").open("a") as source:
            source.write("\n# changed release input\n")
        with self.assertRaisesRegex(ValueError, "Bump the add-on version"):
            build_repository(self.root, self.output)
        after = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(snapshot, after)

    def test_a_version_bump_adds_a_package_without_removing_the_previous_version(self):
        first = build_repository(self.root, self.output)
        old_package = first["addon_zip"].read_bytes()
        manifest = self.root / "addon.xml"
        manifest.write_text(manifest.read_text().replace('version="0.1.0"', 'version="0.1.1"'))
        next_release = build_repository(self.root, self.output)
        self.assertEqual(next_release["addon_zip"].name, "script.silo-0.1.1.zip")
        self.assertEqual(first["addon_zip"].read_bytes(), old_package)
        catalog = ET.fromstring(next_release["index"].read_bytes())
        self.assertEqual(catalog.find("addon[@id='script.silo']").get("version"), "0.1.1")

    def test_legacy_repository_definition_and_wrong_endpoints_are_rejected(self):
        path = self.root / "repository" / REPOSITORY_ID / "addon.xml"
        original = path.read_text()
        path.write_text(original.replace("<dir>", "").replace("</dir>", ""))
        with self.assertRaisesRegex(ValueError, "modern <dir>"):
            validate_repository(self.root)
        path.write_text(original.replace("https://slimshizn.github.io/Silo-Theater/zips/", "https://example.invalid/zips/"))
        with self.assertRaisesRegex(ValueError, "datadir URL"):
            validate_repository(self.root)

    def test_missing_landing_template_fails_before_writing_output(self):
        (self.root / "site/index.html").unlink()
        with self.assertRaisesRegex(ValueError, "site/index.html"):
            build_repository(self.root, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
