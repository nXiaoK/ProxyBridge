import plistlib
import tempfile
import unittest
from pathlib import Path

import app
from desktop import tray_host
import proxybridge_version


ROOT = Path(__file__).resolve().parents[1]


class VersioningTests(unittest.TestCase):
    def test_repository_version_is_the_runtime_default(self) -> None:
        base_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

        self.assertEqual(base_version, "0.3.0")
        self.assertEqual(
            proxybridge_version.resolve_version(root=ROOT, environ={}),
            base_version,
        )
        self.assertEqual(app.APP_VERSION, proxybridge_version.VERSION)
        self.assertEqual(tray_host.APP_VERSION, proxybridge_version.VERSION)
        self.assertEqual(app.AppHandler.server_version, f"ProxyBridge/{base_version}")

    def test_build_environment_can_inject_a_release_version(self) -> None:
        version = proxybridge_version.resolve_version(
            root=ROOT,
            environ={proxybridge_version.VERSION_ENV: "0.3.0-build.42"},
        )

        self.assertEqual(version, "0.3.0-build.42")

    def test_invalid_version_is_rejected(self) -> None:
        for value in ("v0.3.0", "0.3", "0.3.0/unsafe", "01.2.3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    proxybridge_version.validate_version(value)

    def test_missing_version_file_uses_development_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(
                proxybridge_version.resolve_version(root=Path(temp_dir), environ={}),
                proxybridge_version.DEVELOPMENT_VERSION,
            )

    def test_macos_source_plist_matches_base_version(self) -> None:
        with (ROOT / "macos" / "ProxyBridgeMenuBar" / "Info.plist").open("rb") as stream:
            plist = plistlib.load(stream)

        self.assertEqual(
            plist["CFBundleShortVersionString"],
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(plist["CFBundleVersion"], "1")

    def test_all_runtime_packages_include_version_files(self) -> None:
        macos_script = (ROOT / "scripts" / "build_menubar_app.sh").read_text(encoding="utf-8")
        windows_script = (ROOT / "scripts" / "build_windows_exe.ps1").read_text(encoding="utf-8")
        linux_script = (ROOT / "scripts" / "build_linux_bin.sh").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn('printf \'%s\\n\' "$APP_VERSION" > "$BRIDGE_DIR/VERSION"', macos_script)
        self.assertIn('"$BundledVersionPath;."', windows_script)
        self.assertIn('--add-data "VERSION:."', linux_script)
        self.assertIn("COPY --chown=proxybridge:proxybridge VERSION proxybridge_version.py ./", dockerfile)


if __name__ == "__main__":
    unittest.main()
