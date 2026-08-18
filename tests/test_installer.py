import json
import os
import shutil
import subprocess
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
        self.assertIn("-Action SourceUpdate", updater)
        self.assertNotIn("git pull", updater)

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

        self.assertIn("http://127.0.0.1:8000/health", launcher)
        self.assertIn("VisionGate is already running", launcher)
        self.assertLess(
            launcher.index("http://127.0.0.1:8000/health"),
            launcher.index("-m uvicorn"),
        )

    def test_public_access_plan_builds_an_https_reverse_proxy(self):
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
                "-Domain",
                "door.example.com",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(plan["public_url"], "https://door.example.com")
        self.assertIn("reverse_proxy 127.0.0.1:8000", plan["caddyfile"])
        self.assertIn("max_size 2MB", plan["caddyfile"])
        self.assertIn("flush_interval -1", plan["caddyfile"])
        self.assertNotIn("tls_insecure_skip_verify", plan["caddyfile"])

    def test_public_access_rejects_a_domain_that_could_inject_caddy_config(self):
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
                "-Domain",
                "door.example.com { respond hacked }",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertNotEqual(result.returncode, 0)

    def test_launcher_starts_public_https_with_only_local_proxy_headers_trusted(self):
        launcher = (ROOT / "Launch VisionGate.bat").read_text(encoding="utf-8")

        self.assertTrue((ROOT / "Configure Online Access.bat").exists())
        self.assertIn(r"scripts\Public Access.ps1", launcher)
        self.assertIn("-Action Start", launcher)
        self.assertIn("--proxy-headers --forwarded-allow-ips 127.0.0.1", launcher)
        self.assertNotIn('--forwarded-allow-ips "*"', launcher)

    def test_updater_keeps_the_public_https_proxy_current(self):
        updater = (ROOT / "Update VisionGate.bat").read_text(encoding="utf-8")

        self.assertIn(r"scripts\Public Access.ps1", updater)
        self.assertIn("-Action Update", updater)


if __name__ == "__main__":
    unittest.main()
