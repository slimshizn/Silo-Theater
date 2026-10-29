"""Validate the static Kodi repository as an installable, immutable release."""

import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from scripts.build import production_files
from scripts.build_repository import DOWNLOAD_BASE, REPOSITORY_ID, build_repository, validate_repository


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
        self.addon_manifest = ET.parse(self.root / "addon.xml").getroot()
        self.repository_manifest = ET.parse(self.root / "repository" / REPOSITORY_ID / "addon.xml").getroot()
        self.addon_version = self.addon_manifest.get("version")
        self.repository_version = self.repository_manifest.get("version")
        self.addon_filename = "script.silo-{}.zip".format(self.addon_version)
        self.repository_filename = "{}-{}.zip".format(REPOSITORY_ID, self.repository_version)
        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_text('<!doctype html><a href="{}">Install</a>\n'.format(self.repository_filename))
        self.output = Path(self.temporary.name) / "pages"

    def test_catalog_contains_both_addons_and_matches_the_exact_md5(self):
        paths = build_repository(self.root, self.output)
        contents = paths["index"].read_bytes()
        catalog = ET.fromstring(contents)
        self.assertEqual(catalog.tag, "addons")
        self.assertEqual([(item.get("id"), item.get("version")) for item in catalog],
                         [(REPOSITORY_ID, self.repository_version), ("script.silo", self.addon_version)])
        self.assertTrue(all(item.get("provider-name") == "slimshizn" for item in catalog))
        expected = hashlib.md5(contents, usedforsecurity=False).hexdigest()
        self.assertEqual(paths["checksum"].read_text(), expected + "\n")
        for path in (paths["installer"], paths["addon_zip"], paths["repository_zip"]):
            self.assertEqual(path.with_suffix(".zip.sha256").read_text(),
                             hashlib.sha256(path.read_bytes()).hexdigest() + "\n")

    def test_installer_and_downloads_have_the_kodi_layout(self):
        paths = build_repository(self.root, self.output)
        self.assertEqual(paths["installer"].name, self.repository_filename)
        self.assertEqual(paths["repository_zip"].relative_to(self.output).as_posix(),
                         "zips/{}/{}".format(REPOSITORY_ID, self.repository_filename))
        self.assertEqual(paths["addon_zip"].relative_to(self.output).as_posix(),
                         "zips/script.silo/{}".format(self.addon_filename))
        self.assertEqual(paths["installer"].read_bytes(), paths["repository_zip"].read_bytes())
        with zipfile.ZipFile(paths["installer"]) as archive:
            self.assertEqual(archive.namelist(), [REPOSITORY_ID + "/LICENSE", REPOSITORY_ID + "/addon.xml"])
            self.assertEqual(archive.read(REPOSITORY_ID + "/LICENSE"), (self.root / "LICENSE").read_bytes())
            manifest = ET.fromstring(archive.read(REPOSITORY_ID + "/addon.xml"))
            self.assertEqual(manifest.get("provider-name"), "slimshizn")
            directory = manifest.find("./extension[@point='xbmc.addon.repository']/dir")
            self.assertIsNotNone(directory)
            self.assertEqual(directory.findtext("info"), DOWNLOAD_BASE + "addons.xml")
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
        version_parts = [int(part) for part in self.addon_version.split(".")]
        version_parts += [0] * max(0, 3 - len(version_parts))
        version_parts[2] += 1
        next_version = ".".join(str(part) for part in version_parts)
        tree = ET.parse(manifest)
        tree.getroot().set("version", next_version)
        tree.write(manifest, encoding="utf-8", xml_declaration=True)
        next_release = build_repository(self.root, self.output)
        self.assertEqual(next_release["addon_zip"].name, "script.silo-{}.zip".format(next_version))
        self.assertEqual(first["addon_zip"].read_bytes(), old_package)
        catalog = ET.fromstring(next_release["index"].read_bytes())
        self.assertEqual(catalog.find("addon[@id='script.silo']").get("version"), next_version)

    def test_legacy_repository_definition_and_wrong_endpoints_are_rejected(self):
        path = self.root / "repository" / REPOSITORY_ID / "addon.xml"
        original = path.read_text()
        path.write_text(original.replace("<dir>", "").replace("</dir>", ""))
        with self.assertRaisesRegex(ValueError, "modern <dir>"):
            validate_repository(self.root)
        path.write_text(original.replace(DOWNLOAD_BASE + "zips/", "https://example.invalid/zips/"))
        with self.assertRaisesRegex(ValueError, "datadir URL"):
            validate_repository(self.root)

    def test_missing_landing_template_fails_before_writing_output(self):
        (self.root / "site/index.html").unlink()
        with self.assertRaisesRegex(ValueError, "site/index.html"):
            build_repository(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_other_site_pages_are_published_beside_the_landing_page(self):
        (self.root / "site/donate.html").write_text("<!doctype html><p>Donate</p>\n")
        paths = build_repository(self.root, self.output)
        self.assertEqual(paths["pages"], [self.output / "donate.html"])
        self.assertEqual((self.output / "donate.html").read_bytes(), (self.root / "site/donate.html").read_bytes())



class WidgetsAddonTests(unittest.TestCase):
    """Widgets ship as their own add-on so Founders Theater is listed once in Kodi."""

    def test_repository_lists_the_widgets_addon_with_its_files(self):
        with tempfile.TemporaryDirectory() as folder:
            build_repository(ROOT, Path(folder))
            catalog = ET.parse(Path(folder) / "addons.xml").getroot()
            ids = sorted(addon.get("id") for addon in catalog)
            self.assertEqual(ids, ["plugin.video.silotheater", REPOSITORY_ID, "script.silo"])
            widgets = catalog.find("addon[@id='plugin.video.silotheater']")
            package = Path(folder) / "zips/plugin.video.silotheater/plugin.video.silotheater-{}.zip".format(
                widgets.get("version"))
            with zipfile.ZipFile(package) as archive:
                self.assertEqual(sorted(archive.namelist()), [
                    "plugin.video.silotheater/LICENSE", "plugin.video.silotheater/addon.xml",
                    "plugin.video.silotheater/default.py", "plugin.video.silotheater/icon.png"])
            silo = catalog.find("addon[@id='script.silo']")
            self.assertIsNone(silo.find("extension[@point='xbmc.python.pluginsource']"))

    def test_widgets_require_this_silo_theater_release(self):
        widgets = ET.parse(ROOT / "widgets/addon.xml").getroot()
        required = widgets.find("requires/import[@addon='script.silo']").get("version")
        current = ET.parse(ROOT / "addon.xml").getroot().get("version")
        version = lambda value: tuple(int(part) for part in value.split("."))
        self.assertLessEqual(version(required), version(current))

    def test_silo_theater_cannot_become_a_plugin_again(self):
        from scripts.build import validate_addon

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            shutil.copytree(ROOT / "resources", root / "resources")
            for name in ("default.py", "service.py", "LICENSE"):
                shutil.copyfile(ROOT / name, root / name)
            manifest = (ROOT / "addon.xml").read_text(encoding="utf-8").replace(
                "</addon>", '  <extension point="xbmc.python.pluginsource" library="default.py">'
                "<provides>video</provides></extension>\n</addon>")
            (root / "addon.xml").write_text(manifest, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "plugin source"):
                validate_addon(root)

if __name__ == "__main__":
    unittest.main()
