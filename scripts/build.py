#!/usr/bin/env python3
"""Validate and package Silo without copying development or user data.

The archive is reproducible for unchanged inputs with the same Python/zlib
toolchain: members are sorted and have fixed timestamps and permissions.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = frozenset({"addon.xml", "default.py", "LICENSE"})
RESOURCE_SUFFIXES = frozenset(
    {".py", ".xml", ".po", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ttf", ".otf"}
)
EXCLUDED_PARTS = frozenset(
    {"__pycache__", "test", "tests", "docs", "documentation", "fixtures", "userdata"}
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def production_files(root: Path) -> List[Path]:
    """Return a sorted allowlist of package inputs, rejecting symlinks.

    Only Python modules, skin/media assets, settings and language files from
    the two runtime directories are allowed. In particular, JSON state,
    credentials, hidden files, bytecode and local documents cannot enter
    the archive through a broad recursive copy.
    """
    root = Path(root).resolve()
    candidates = [root / name for name in ROOT_FILES]
    for directory in (root / "lib", root / "resources"):
        if directory.is_symlink():
            raise ValueError("Package inputs cannot be symlinks: {}".format(directory.name))
        if directory.is_dir():
            candidates.extend(directory.rglob("*"))

    result = []
    for path in candidates:
        relative = path.relative_to(root)
        if any(part.startswith(".") or part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Package inputs cannot be symlinks: {}".format(relative))
        if not path.is_file():
            continue
        if len(relative.parts) == 1:
            allowed = relative.name in ROOT_FILES
        elif relative.parts[0] == "lib":
            allowed = path.suffix == ".py"
        else:
            allowed = path.suffix.lower() in RESOURCE_SUFFIXES
        if allowed:
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def _xml(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ValueError("Invalid XML {}: {}".format(path.name, error)) from error


def validate_addon(root: Path) -> Dict[str, str]:
    """Check manifest, runtime syntax and settings; return id/version/name.

    This is a structural check. Kodi rendering and live server compatibility
    still need validation in Kodi and against the intended Silo server.
    """
    root = Path(root).resolve()
    for name in ROOT_FILES:
        if not (root / name).is_file():
            raise ValueError("Required package file is missing: {}".format(name))

    manifest = _xml(root / "addon.xml")
    if manifest.tag != "addon":
        raise ValueError("addon.xml must have an <addon> root")
    addon_id = manifest.get("id", "")
    version = manifest.get("version", "")
    if addon_id != "script.silo":
        raise ValueError("The add-on id must be script.silo")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
        raise ValueError("The add-on version must be a numeric dotted version")
    if not manifest.get("name", "").strip():
        raise ValueError("The add-on name is missing")

    dependencies = {
        item.get("addon"): item.get("version")
        for item in manifest.findall("./requires/import")
    }
    if dependencies.get("xbmc.python") != "3.0.1":
        raise ValueError("The Kodi Python dependency must be 3.0.1")
    if dependencies.get("xbmc.gui") != "5.17.0":
        raise ValueError("The Kodi GUI dependency must be 5.17.0 (Kodi 21)")
    scripts = manifest.findall("./extension[@point='xbmc.python.script']")
    if len(scripts) != 1 or scripts[0].get("library") != "default.py":
        raise ValueError("The script entry point must be default.py")
    metadata = manifest.find("./extension[@point='xbmc.addon.metadata']")
    if metadata is None or metadata.findtext("license") != "AGPL-3.0-or-later":
        raise ValueError("The add-on license must be AGPL-3.0-or-later")

    settings_path = root / "resources/settings.xml"
    settings = _xml(settings_path)
    if settings.tag != "settings" or settings.get("version") != "1":
        raise ValueError("Settings must use Kodi's version 1 settings format")
    sections = settings.findall("section")
    if len(sections) != 1 or sections[0].get("id") != addon_id:
        raise ValueError("Settings need one section matching the add-on id")
    seen = set()
    for setting in settings.findall(".//setting"):
        setting_id = setting.get("id", "")
        if not setting_id or setting_id in seen:
            raise ValueError("Setting ids must be nonempty and unique: {}".format(setting_id))
        seen.add(setting_id)
        if setting.find("default") is None or setting.find("control") is None:
            raise ValueError("Setting needs a default and control: {}".format(setting_id))
    required_settings = {"server_url", "sign_in_method", "dv_mode", "auto_next", "progress_interval"}
    if not required_settings.issubset(seen):
        raise ValueError("Required settings are missing: {}".format(
            ", ".join(sorted(required_settings - seen))
        ))
    language_path = root / "resources/language/resource.language.en_gb/strings.po"
    if not language_path.is_file():
        raise ValueError("English settings strings are missing")
    strings = language_path.read_text(encoding="utf-8")
    localized_ids = set(re.findall(r'^msgctxt "#([0-9]+)"$', strings, re.MULTILINE))
    for element in settings.iter():
        for attribute in ("label", "help"):
            label = element.get(attribute, "")
            if label.isdigit() and int(label) >= 30000 and label not in localized_ids:
                raise ValueError("Missing English setting string: {}".format(label))
        if element.tag == "heading" and (element.text or "") not in localized_ids:
            raise ValueError("Missing English heading string: {}".format(element.text))

    for path in production_files(root):
        relative = path.relative_to(root).as_posix()
        if path.suffix == ".py":
            try:
                compile(path.read_bytes(), relative, "exec")
            except (SyntaxError, ValueError) as error:
                raise ValueError("Invalid Python {}: {}".format(relative, error)) from error
        elif path.suffix == ".xml":
            _xml(path)
    return {"id": addon_id, "version": version, "name": manifest.get("name", "")}


def build_addon(root: Path, output_dir: Optional[Path] = None) -> Path:
    """Validate and atomically write one installable Kodi add-on ZIP."""
    root = Path(root).resolve()
    metadata = validate_addon(root)
    target_dir = Path(output_dir) if output_dir is not None else root / "dist"
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / "{id}-{version}.zip".format(**metadata)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target_dir, suffix=".zip", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in production_files(root):
                name = metadata["id"] + "/" + path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compresslevel=9)
        os.replace(str(temporary_path), str(destination))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate without writing a ZIP")
    parser.add_argument("--output-dir", type=Path, help="ZIP destination directory (default: dist/)")
    args = parser.parse_args()
    try:
        if args.check:
            metadata = validate_addon(ROOT)
            print("Validated {id} {version}".format(**metadata))
        else:
            print(build_addon(ROOT, args.output_dir))
    except (OSError, ValueError) as error:
        parser.exit(1, "Build failed: {}\n".format(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
