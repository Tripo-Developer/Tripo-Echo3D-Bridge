"""Release zip and manifest checks. No network and no PyInstaller build."""

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

import echo3d_bridge
from echo3d_bridge import auto_update as update


def load_release_package():
    path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release_package.py"
    spec = importlib.util.spec_from_file_location("echo_release_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleasePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = load_release_package()

    def test_source_tag_matches_package_version(self):
        self.assertEqual(self.release.source_record(), echo3d_bridge.__version__)
        self.assertEqual(self.release.source_tag(), f"v{echo3d_bridge.__version__}")

    def test_wrong_tag_is_rejected(self):
        with self.assertRaises(ValueError):
            self.release.check_source("v9.9.9")

    def test_pack_writes_the_updater_contract(self):
        tag = self.release.source_tag()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "Tripo-Echo3D-Bridge.exe"
            exe.write_bytes(b"MZ" + b"\0" * 64)
            archive = self.release.pack(exe, tag, root / "out")
            update.archive_info(archive, tag)
            self.assertEqual(archive.name, f"Tripo3d_echo3d_Bridge-{tag}.zip")
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(set(package.namelist()), {update.EXE_NAME, update.VERSION_NAME})

    def test_manifest_points_at_the_versioned_zip(self):
        tag = self.release.source_tag()
        url = f"https://tripo-public.tripo3d.ai/plugins/echo3d-bridge/Tripo3d_echo3d_Bridge-{tag}.zip"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "Tripo-Echo3D-Bridge.exe"
            exe.write_bytes(b"MZ" + b"\0" * 32)
            archive = self.release.pack(exe, tag, root)
            manifest = self.release.manifest_for(archive, tag, url)
        self.assertEqual(manifest["version"], echo3d_bridge.__version__)
        self.assertEqual(manifest["channel"], "intl")
        self.assertEqual(manifest["id"], "echo3d-bridge")
        self.assertEqual(manifest["packages"]["default"]["url"], url)
        self.assertEqual(len(manifest["packages"]["default"]["sha256"]), 64)

    def test_stage_mac_keeps_the_apple_silicon_disk_image(self):
        tag = self.release.source_tag()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "incoming.zip"
            with zipfile.ZipFile(source, "w") as package:
                package.writestr("Tripo-Echo3D-Bridge-macOS-arm64.dmg", b"arm")
            staged = self.release.stage_mac(source, tag, root / "out")
            self.assertEqual(staged.name, f"Tripo3d_echo3d_Bridge-mac-{tag}.zip")
            with zipfile.ZipFile(staged) as package:
                self.assertEqual(set(package.namelist()), {"Tripo-Echo3D-Bridge-macOS-arm64.dmg"})

    def test_stage_mac_rejects_a_readme(self):
        tag = self.release.source_tag()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "incoming.zip"
            with zipfile.ZipFile(source, "w") as package:
                package.writestr("Tripo-Echo3D-Bridge-macOS-arm64.dmg", b"arm")
                package.writestr("README.md", b"install")
            with self.assertRaises(ValueError):
                self.release.stage_mac(source, tag, root / "out")

    def test_macos_package_follows_the_disk_image_layout(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "build.sh").read_text(encoding="utf-8")
        workflow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("codesign --force --deep --sign -", script)
        self.assertIn("hdiutil create", script)
        self.assertIn("-format UDRW", script)
        self.assertIn('ln -s /Applications', script)
        self.assertIn('${name}-macOS-${arch}.dmg', script)
        self.assertIn('volume_name="Tripo Echo3D Bridge"', script)
        self.assertIn('"$arch" != "arm64"', script)
        self.assertIn("runs-on: macos-15", workflow)
        self.assertNotIn("macos-15-intel", workflow)
        self.assertIn("Tripo-Echo3D-Bridge-macOS-arm64.dmg", workflow)
        self.assertNotIn("x86_64", workflow)
        self.assertNotIn("README.md", workflow)

    def test_stage_mac_rejects_a_windows_package(self):
        tag = self.release.source_tag()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "Tripo-Echo3D-Bridge.exe"
            exe.write_bytes(b"MZ" + b"\0" * 16)
            windows = self.release.pack(exe, tag, root)
            with self.assertRaises(ValueError):
                self.release.stage_mac(windows, tag, root / "out")


if __name__ == "__main__":
    unittest.main()
