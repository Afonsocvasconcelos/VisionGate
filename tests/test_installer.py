import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts" / "Setup VisionGate.ps1"


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

    def test_general_requirements_do_not_force_a_gpu_backend(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        installer = (ROOT / "Install VisionGate.bat").read_text(encoding="utf-8")
        launcher = (ROOT / "Launch VisionGate.bat").read_text(encoding="utf-8")
        updater = (ROOT / "Update VisionGate.bat").read_text(encoding="utf-8")

        self.assertNotIn("download.pytorch.org", requirements)
        self.assertNotIn("torch==", requirements)
        self.assertNotIn("goto :no_git", updater)
        self.assertIn(r"scripts\Setup VisionGate.ps1", installer)
        self.assertIn(r"scripts\Setup VisionGate.ps1", launcher)
        self.assertIn(r"scripts\Setup VisionGate.ps1", updater)
        self.assertTrue((ROOT / "Install VisionGate.bat").exists())
        self.assertTrue((ROOT / "Update VisionGate.bat").exists())


if __name__ == "__main__":
    unittest.main()
