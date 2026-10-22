#!/usr/bin/env python3
"""Build deterministic Kodi repository files for the Founders Theater website."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
from typing import Dict, Optional
import xml.etree.ElementTree as ET
import zipfile

try:
    from .build import ROOT, ZIP_TIMESTAMP, build_addon, validate_addon
except ImportError:
    from build import ROOT, ZIP_TIMESTAMP, build_addon, validate_addon


REPOSITORY_ID = "repository.silo.theater"
PAGES_BASE = "https://slimshizn.github.io/Silo-Theater/"
DOWNLOAD_BASE = "https://raw.githubusercontent.com/slimshizn/Silo-Theater/gh-pages/"


def validate_repository(root: Path) -> ET.Element:
    """Validate the installer manifest against its fixed public endpoint."""
    root = Path(root).resolve()
    source = root / "repository" / REPOSITORY_ID
    for path in (source, source / "addon.xml", root / "LICENSE"):
        if path.is_symlink():
            raise ValueError("Repository inputs cannot be symlinks: {}".format(path.name))
    try:
        manifest = ET.parse(source / "addon.xml").getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError("Invalid repository addon.xml: {}".format(error)) from error
    if manifest.tag != "addon" or manifest.get("id") != REPOSITORY_ID:
        raise ValueError("The repository id must be {}".format(REPOSITORY_ID))
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", manifest.get("version", "")):
        raise ValueError("The repository version must be a numeric dotted version")
    if not manifest.get("name", "").strip():
        raise ValueError("The repository name is missing")
    metadata = manifest.find("./extension[@point='xbmc.addon.metadata']")
    if metadata is None or metadata.findtext("license") != "AGPL-3.0-or-later":
        raise ValueError("The repository license must be AGPL-3.0-or-later")
    if not (root / "LICENSE").is_file():
        raise ValueError("The repository LICENSE is missing")
    extensions = manifest.findall("./extension[@point='xbmc.addon.repository']")
    if len(extensions) != 1:
        raise ValueError("The repository needs one repository extension")
    extension = extensions[0]
    directories = extension.findall("dir")
    if len(directories) != 1 or extension.find("info") is not None:
        raise ValueError("The repository must use one modern <dir> definition")
    directory = directories[0]
    expected_urls = {"info": "addons.xml", "checksum": "addons.xml.md5", "datadir": "zips/"}
    for element, suffix in expected_urls.items():
        if directory.findtext(element) != DOWNLOAD_BASE + suffix:
            raise ValueError("The repository {} URL must point to {}{}".format(element, DOWNLOAD_BASE, suffix))
    info = directory.find("info")
    checksum = directory.find("checksum")
    datadir = directory.find("datadir")
    if info.get("compressed") != "false" or datadir.get("zip") != "true":
        raise ValueError("The repository needs uncompressed XML and zipped packages")
    if checksum.get("verify") != "md5" or directory.findtext("hashes") != "sha256":
        raise ValueError("The repository must verify its MD5 index and SHA-256 packages")
    return manifest


def _repository_zip(root: Path, manifest: ET.Element) -> bytes:
    buffer = io.BytesIO()
    files = {
        "LICENSE": (root / "LICENSE").read_bytes(),
        "addon.xml": (root / "repository" / REPOSITORY_ID / "addon.xml").read_bytes(),
    }
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, contents in sorted(files.items()):
            info = zipfile.ZipInfo(manifest.get("id") + "/" + name, date_time=ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, contents, compresslevel=9)
    return buffer.getvalue()


def _write(path: Path, data: bytes) -> None:
    """Replace one generated file atomically, leaving unrelated files intact."""
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


WIDGETS_ID = "plugin.video.silotheater"
WIDGETS_FILES = ("addon.xml", "default.py")


def _widgets(root: Path):
    """The optional widgets add-on: (manifest, zip bytes), or None without widgets/."""
    folder = root / "widgets"
    if not folder.is_dir():
        return None
    manifest = ET.parse(folder / "addon.xml").getroot()
    if manifest.get("id") != WIDGETS_ID or not manifest.get("version"):
        raise ValueError("widgets/addon.xml must declare {} with a version".format(WIDGETS_ID))
    compile((folder / "default.py").read_text(encoding="utf-8"), "widgets/default.py", "exec")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        sources = [(name, folder / name) for name in WIDGETS_FILES] + [("LICENSE", root / "LICENSE")]
        for name, path in sources:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Widgets package input is missing or a symlink: {}".format(name))
            info = zipfile.ZipInfo(WIDGETS_ID + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return manifest, buffer.getvalue()


def build_repository(root: Path = ROOT, output_dir: Optional[Path] = None) -> Dict[str, Path]:
    """Build the Kodi catalog, installer and versioned add-on packages.

    Existing versioned ZIPs are immutable: changed bytes require a version
    bump. Older packages and an existing website index are left in place.
    This guard applies to files already present in the selected output folder.
    """
    root = Path(root).resolve()
    output = Path(output_dir).resolve() if output_dir is not None else root / "dist/pages"
    template = root / "site/index.html"
    if not template.is_file() or template.is_symlink():
        raise ValueError("The website template site/index.html is missing or is a symlink")
    landing_page = template.read_bytes()
    addon = validate_addon(root)
    repository_manifest = validate_repository(root)
    repository_version = repository_manifest.get("version")
    with tempfile.TemporaryDirectory(prefix="silo-repository-") as temporary:
        addon_zip = build_addon(root, Path(temporary)).read_bytes()
    repository_zip = _repository_zip(root, repository_manifest)
    addon_name = "{id}-{version}.zip".format(**addon)
    repository_name = "{}-{}.zip".format(REPOSITORY_ID, repository_version)
    widgets = _widgets(root)
    packages = {
        output / "zips" / addon["id"] / addon_name: addon_zip,
        output / "zips" / REPOSITORY_ID / repository_name: repository_zip,
        output / repository_name: repository_zip,
    }
    if widgets is not None:
        widgets_name = "{}-{}.zip".format(WIDGETS_ID, widgets[0].get("version"))
        packages[output / "zips" / WIDGETS_ID / widgets_name] = widgets[1]
        # Validate all immutable destinations before updating any catalog or file.
    for path, contents in packages.items():
        if path.exists() and path.read_bytes() != contents:
            raise ValueError("Package already exists with different bytes: {}. Bump the add-on version before rebuilding.".format(path.name))

    catalog = ET.Element("addons")
    manifests = [ET.parse(root / "addon.xml").getroot(), repository_manifest]
    if widgets is not None:
        manifests.append(widgets[0])
    for manifest in sorted(manifests, key=lambda value: value.get("id", "")):
        catalog.append(deepcopy(manifest))
    ET.indent(catalog, space="  ")
    catalog_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(catalog, encoding="utf-8") + b"\n"
    # Kodi requests these legacy MD5 catalog checksums; package integrity uses SHA-256.
    checksum = hashlib.md5(catalog_bytes, usedforsecurity=False).hexdigest().encode("ascii") + b"\n"
    for path, contents in packages.items():
        _write(path, contents)
        _write(path.with_suffix(path.suffix + ".sha256"), hashlib.sha256(contents).hexdigest().encode("ascii") + b"\n")
    # Kodi's repository browser shows each add-on's icon from datadir before
    # install. Icons are not versioned, so they are replaced on every build.
    icons = {output / "zips" / addon["id"] / "resources" / "icon.png": root / "resources" / "icon.png"}
    if widgets is not None:
        icons[output / "zips" / WIDGETS_ID / "icon.png"] = root / "widgets" / "icon.png"
    for path, source in icons.items():
        if source.is_file():  # The add-ons currently use Kodi's default icon.
            _write(path, source.read_bytes())
    _write(output / "addons.xml", catalog_bytes)
    _write(output / "addons.xml.md5", checksum)
    _write(output / "index.html", landing_page)
    _write(output / ".nojekyll", b"")
    return {
        "index": output / "addons.xml",
        "checksum": output / "addons.xml.md5",
        "installer": output / repository_name,
        "addon_zip": output / "zips" / addon["id"] / addon_name,
        "repository_zip": output / "zips" / REPOSITORY_ID / repository_name,
        "landing_page": output / "index.html",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="website output directory (default: dist/pages)")
    args = parser.parse_args()
    try:
        paths = build_repository(ROOT, args.output_dir)
        print(paths["installer"])
    except (OSError, ValueError) as error:
        parser.exit(1, "Repository build failed: {}\n".format(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
