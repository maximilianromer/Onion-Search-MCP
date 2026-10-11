"""Exercise the dependency check against a real, isolated pip environment."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import venv


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class RequirementsCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.venv_dir = cls.root / ".venv"
        venv.EnvBuilder(with_pip=True).create(cls.venv_dir)
        python = cls.venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cls.site_packages = Path(subprocess.check_output(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def check(self, requirements, version="9.16.0", primp_version="2.0.1"):
        # Installed metadata is sufficient for pip's version resolution. There
        # is no network access, package download or modification of real DDGS.
        for package, installed_version in (("ddgs", version), ("primp", primp_version)):
            for old_metadata in self.site_packages.glob(f"{package}-*.dist-info"):
                shutil.rmtree(old_metadata)
            metadata = self.site_packages / f"{package}-{installed_version}.dist-info"
            metadata.mkdir()
            (metadata / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: {package}\nVersion: {installed_version}\n",
                encoding="utf-8",
            )
        (self.root / "requirements-macos.txt").write_text(requirements, encoding="utf-8")
        with patch.object(installer, "SCRIPT_DIR", self.root), patch.object(
            installer, "VENV_DIR", self.venv_dir
        ):
            return installer.check_requirements_installed("macos")

    def test_old_ddgs_requires_upgrade(self):
        self.assertFalse(self.check("ddgs>=9.16.0\n", version="9.14.4"))

    def test_old_http_client_requires_upgrade(self):
        self.assertFalse(self.check("ddgs>=9.16.0\nprimp>=2.0.1\n", primp_version="1.3.1"))

    def test_current_ddgs_can_skip_install(self):
        self.assertTrue(self.check("ddgs>=9.16.0\n"))

    def test_missing_dependency_requires_install(self):
        self.assertFalse(self.check("ddgs>=9.16.0\nonion-test-missing-dependency>=1\n"))

    def test_comments_case_and_environment_markers(self):
        self.assertTrue(self.check(
            '# Dependencies\nDDGS>=9.16.0 # search fix\n'
            'onion-test-missing-dependency; python_version < "3.0"\n'
        ))

    def test_project_requires_fixed_search_dependencies_on_every_platform(self):
        for platform in ("macos", "linux", "windows"):
            with self.subTest(platform=platform):
                requirements = (ROOT / f"requirements-{platform}.txt").read_text()
                search_requirements = "\n".join(
                    line for line in requirements.splitlines()
                    if line.startswith(("ddgs", "primp"))
                ) + "\n"
                self.assertFalse(self.check(search_requirements, version="9.14.4"))
                self.assertFalse(self.check(search_requirements, primp_version="1.3.1"))
                self.assertTrue(self.check(search_requirements))


if __name__ == "__main__":
    unittest.main()
