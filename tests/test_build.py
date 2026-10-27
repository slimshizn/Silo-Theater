"""Offline release checks: reproducibility and strict production inputs."""

import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("silo_build", ROOT / "scripts/build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        for name in ("addon.xml", "LICENSE"):
            shutil.copyfile(ROOT / name, self.root / name)
        self.manifest = ET.parse(self.root / "addon.xml").getroot()
        self.version = self.manifest.get("version")
        shutil.copytree(ROOT / "resources", self.root / "resources")
        (self.root / "default.py").write_text("print('Silo')\n", encoding="utf-8")
        (self.root / "service.py").write_text("print('Silo service')\n", encoding="utf-8")

    def write(self, name, content="private data"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_installable_archive_excludes_development_state_and_bytecode(self):
        for name in (
            "private-notes.md", "tests/test_secret.py", ".env",
            "resources/session.json", "resources/.credentials.xml",
            "resources/userdata/settings.xml", "resources/lib/__pycache__/api.pyc",
            "resources/docs/secret.xml", "lib/tests/test_api.py",
        ):
            self.write(name)
        self.write("resources/lib/client.py", "VALUE = 1\n")
        archive_path = build.build_addon(self.root)
        self.assertEqual(archive_path.name, "script.silo-{}.zip".format(self.version))
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertIn("script.silo/addon.xml", names)
            self.assertEqual(ET.fromstring(archive.read("script.silo/addon.xml")).get("provider-name"), "slimshizn")
            self.assertIn("script.silo/default.py", names)
            self.assertIn("script.silo/LICENSE", names)
            self.assertIn("script.silo/resources/lib/client.py", names)
            self.assertTrue(all(name.startswith("script.silo/") for name in names))
            self.assertFalse(any("private data" in archive.read(name).decode("utf-8", errors="ignore") for name in names))
            self.assertEqual(names, sorted(names))
            self.assertIsNone(archive.testzip())

    def test_build_is_byte_for_byte_reproducible_after_mtime_and_mode_change(self):
        first = build.build_addon(self.root).read_bytes()
        source = self.root / "default.py"
        os.utime(source, (1740000000, 1740000000))
        source.chmod(0o755)
        second = build.build_addon(self.root).read_bytes()
        self.assertEqual(first, second)

    def test_symlink_cannot_package_files_outside_the_source(self):
        private = Path(self.temporary.name) / "private.py"
        private.write_text("TOKEN = 'private'\n", encoding="utf-8")
        module = self.root / "resources/lib/leak.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.symlink_to(private)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            build.build_addon(self.root)

    def test_manifest_cannot_change_the_archive_root_to_a_traversal_path(self):
        manifest = self.root / "addon.xml"
        manifest.write_text(manifest.read_text().replace('id="script.silo"', 'id="../private"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "id must"):
            build.build_addon(self.root)

    def test_missing_entry_point_is_reported_before_packaging(self):
        (self.root / "default.py").unlink()
        with self.assertRaisesRegex(ValueError, "default.py"):
            build.build_addon(self.root)

    def test_startup_service_is_packaged_and_required_when_declared(self):
        with zipfile.ZipFile(build.build_addon(self.root)) as archive:
            self.assertIn("script.silo/service.py", archive.namelist())
        (self.root / "service.py").unlink()
        with self.assertRaisesRegex(ValueError, "service.py"):
            build.build_addon(self.root)

    def test_python_and_xml_errors_are_reported_before_packaging(self):
        self.write("resources/lib/broken.py", "def broken(:\n")
        with self.assertRaisesRegex(ValueError, "Invalid Python"):
            build.build_addon(self.root)
        self.write("resources/lib/broken.py", "VALUE = 1\n")
        self.write("resources/skins/Main/1080i/broken.xml", "<window>")
        with self.assertRaisesRegex(ValueError, "Invalid XML"):
            build.build_addon(self.root)

    def test_settings_labels_must_have_english_translations(self):
        settings = self.root / "resources/settings.xml"
        settings.write_text(settings.read_text().replace('label="32001"', 'label="32999"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Missing English setting string: 32999"):
            build.build_addon(self.root)


if __name__ == "__main__":
    unittest.main()
