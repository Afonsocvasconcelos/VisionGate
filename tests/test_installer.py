import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts" / "Setup VisionGate.ps1"
PUBLIC_SETUP = ROOT / "scripts" / "Public Access.ps1"


class InstallerTests(unittest.TestCase):
    def plan(self, backend: str) -> dict:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SETUP),
                "-Action",
                "Plan",
                "-Backend",
                backend,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_cpu_plan_uses_the_official_cpu_wheels(self):
        plan = self.plan("CPU")

        self.assertEqual(plan["backend"], "cpu")
        self.assertEqual(plan["torch_index"], "https://download.pytorch.org/whl/cpu")

    def test_nvidia_plan_uses_cuda_wheels(self):
        plan = self.plan("CUDA")

        self.assertEqual(plan["backend"], "cuda")
        self.assertEqual(plan["torch_index"], "https://download.pytorch.org/whl/cu126")

    def test_microsoft_store_python_alias_does_not_abort_before_winget_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "VisionGate"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            setup = scripts / SETUP.name
            shutil.copy(SETUP, setup)
            shutil.copy(
                Path(os.environ["WINDIR"]) / "System32" / "where.exe",
                Path(directory) / "winget.exe",
            )
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join(
                (
                    directory,
                    str(
                        Path(os.environ["LOCALAPPDATA"])
                        / "Microsoft"
                        / "WindowsApps"
                    ),
                )
            )
            environment["LOCALAPPDATA"] = directory
            environment["VISIONGATE_BACKEND"] = "CPU"

            result = subprocess.run(
                [
                    str(
                        Path(os.environ["WINDIR"])
                        / "System32"
                        / "WindowsPowerShell"
                        / "v1.0"
                        / "powershell.exe"
                    ),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(setup),
                    "-Action",
                    "Install",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Installing Python 3.11", result.stdout)
        self.assertNotIn("Python was not found", result.stdout)

    def test_general_requirements_do_not_force_a_gpu_backend(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        setup_script = SETUP.read_text(encoding="utf-8")
        installer = (ROOT / "Install VisionGate.bat").read_text(encoding="utf-8")
        launcher = (ROOT / "Launch VisionGate.bat").read_text(encoding="utf-8")
        login_configurator = (ROOT / "Configure Login.bat").read_text(encoding="utf-8")
        updater = (ROOT / "Update VisionGate.bat").read_text(encoding="utf-8")

        self.assertNotIn("download.pytorch.org", requirements)
        self.assertNotIn("torch==", requirements)
        check_action = setup_script.split('if ($Action -eq "Check")', 1)[1].split(
            "exit 0", 1
        )[0]
        self.assertNotIn("import torch", check_action)
        self.assertIn("find_spec", check_action)
        self.assertIn('if ($Action -in @("Install", "Update", "Plan") -and $Backend -eq "Auto")', setup_script)
        self.assertNotIn("goto :no_git", updater)
        self.assertIn(r"scripts\Setup VisionGate.ps1", installer)
        self.assertIn(r"scripts\Setup VisionGate.ps1", launcher)
        self.assertIn("auth.py --ensure", launcher)
        self.assertIn("auth.py", login_configurator)
        self.assertIn(r"scripts\Setup VisionGate.ps1", updater)
        self.assertTrue((ROOT / "Configure Login.bat").exists())
        self.assertTrue((ROOT / "Install VisionGate.bat").exists())
        self.assertTrue((ROOT / "Update VisionGate.bat").exists())

    def test_updater_can_bootstrap_git_for_application_updates(self):
        setup_script = SETUP.read_text(encoding="utf-8")
        updater = (ROOT / "Update VisionGate.bat").read_text(encoding="utf-8")

        self.assertIn('if ($Action -eq "SourceUpdate")', setup_script)
        self.assertIn("Git.Git", setup_script)
        self.assertIn("-Action Backup", updater)
        self.assertLess(updater.index("-Action Backup"), updater.index("-Action SourceUpdate"))
        self.assertIn("-Action SourceUpdate", updater)
        self.assertNotIn("git pull", updater)

    def test_setup_checks_every_application_module_and_preserves_runtime_data(self):
        setup_script = SETUP.read_text(encoding="utf-8")
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        for module in (
            "app.py",
            "auth.py",
            "automation.py",
            "core.py",
            "enrollment.py",
            "ewelink_cloud.py",
            "ewelink_devices.py",
        ):
            self.assertIn(module, setup_script)
        self.assertIn('Join-Path $root ".env"', setup_script)
        self.assertIn('Join-Path $root "data"', setup_script)
        self.assertIn("backups/", ignore)

    def test_launcher_can_query_the_lan_address_without_cmd_quote_corruption(self):
        launcher = (ROOT / "Launch VisionGate.bat").read_text(encoding="utf-8")
        address_line = next(
            line
            for line in launcher.splitlines()
            if line.startswith('for /f "delims="') and "local_ipv4_addresses" in line
        )
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe.bat"
            probe.write_text(
                "@echo off\n"
                "setlocal\n"
                f'cd /d "{ROOT}"\n'
                'set "VISIONGATE_PYTHON=.venv\\Scripts\\python.exe"\n'
                'set "VISIONGATE_LAN="\n'
                f"{address_line}\n"
                "if not defined VISIONGATE_LAN exit /b 9\n"
                "echo %VISIONGATE_LAN%\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(probe)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("is not recognized", result.stdout + result.stderr)

    def test_launcher_reuses_an_existing_healthy_server_before_starting_uvicorn(self):
        launcher = (ROOT / "Launch VisionGate.bat").read_text(encoding="utf-8")

        self.assertIn("http://127.0.0.1:83/health", launcher)
        self.assertIn("VisionGate is already running", launcher)
        self.assertLess(
            launcher.index("http://127.0.0.1:83/health"),
            launcher.index("-m uvicorn"),
        )

    def test_public_access_plan_uses_direct_http_port_83(self):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PUBLIC_SETUP),
                "-Action",
                "Plan",
                "-HostName",
                "door.example.com",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(plan["public_url"], "http://door.example.com:83")
        self.assertEqual(plan["port"], 83)
        self.assertEqual(plan["allowed_host"], "door.example.com")
        self.assertNotIn("caddyfile", plan)

    def test_public_access_rejects_an_invalid_public_host(self):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PUBLIC_SETUP),
                "-Action",
                "Plan",
                "-HostName",
                "door.example.com { respond hacked }",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertNotEqual(result.returncode, 0)

    def test_public_access_accepts_a_public_ipv4_address(self):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PUBLIC_SETUP),
                "-Action",
                "Plan",
                "-HostName",
                "203.0.113.10",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            json.loads(result.stdout.strip().splitlines()[-1])["public_url"],
            "http://203.0.113.10:83",
        )

    def test_public_host_setting_is_automatically_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["DATA_DIR"] = directory
            environment["DISABLE_VISION"] = "1"
            environment["VISIONGATE_PUBLIC_HOST"] = "cacv.dyndns.org"
            environment["VISIONGATE_ALLOWED_HOSTS"] = ""
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from fastapi.testclient import TestClient; from app import app; "
                    "response=TestClient(app, base_url='http://cacv.dyndns.org:83').get('/login'); "
                    "raise SystemExit(0 if response.status_code == 200 else 1)",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_launcher_uses_only_direct_http_port_83(self):
        launcher = (ROOT / "Launch VisionGate.bat").read_text(encoding="utf-8")
        public_setup = PUBLIC_SETUP.read_text(encoding="utf-8")

        self.assertTrue((ROOT / "Configure Online Access.bat").exists())
        self.assertIn("--host 0.0.0.0 --port 83", launcher)
        self.assertIn("localport=83", launcher)
        self.assertIn('delete rule name="VisionGate"', public_setup)
        self.assertIn('localport=83 profile=any', public_setup)
        self.assertNotIn("--proxy-headers", launcher)
        self.assertNotIn("https://", launcher.lower())

    def test_updater_has_no_https_proxy_dependency(self):
        updater = (ROOT / "Update VisionGate.bat").read_text(encoding="utf-8")

        self.assertNotIn(r"scripts\Public Access.ps1", updater)
        self.assertNotIn("Caddy", updater)


if __name__ == "__main__":
    unittest.main()
