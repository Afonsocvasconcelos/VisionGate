param(
    [ValidateSet("Install", "Update", "Check", "Plan")]
    [string]$Action = "Install",
    [ValidateSet("Auto", "CPU", "CUDA")]
    [string]$Backend = "Auto"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$torchVersion = "2.12.1"
$torchvisionVersion = "0.27.1"

function Invoke-NativeProbe {
    param([scriptblock]$Command)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        $output = & $Command 2>$null
        return [pscustomobject]@{
            Succeeded = $LASTEXITCODE -eq 0
            Output = $output
        }
    } catch {
        return [pscustomobject]@{ Succeeded = $false; Output = $null }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

if ($Backend -eq "Auto" -and $env:VISIONGATE_BACKEND -in @("CPU", "CUDA")) {
    $Backend = $env:VISIONGATE_BACKEND
}
if ($Backend -eq "Auto") {
    $nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($nvidia) {
        $probe = Invoke-NativeProbe { & $nvidia.Source --query-gpu=name --format=csv,noheader }
        $Backend = if ($probe.Succeeded) { "CUDA" } else { "CPU" }
    } else {
        $Backend = "CPU"
    }
}
$backendName = $Backend.ToLowerInvariant()
$torchIndex = "https://download.pytorch.org/whl/" + $(if ($Backend -eq "CUDA") { "cu126" } else { "cpu" })

if ($Action -eq "Plan") {
    [pscustomobject]@{
        backend = $backendName
        torch_index = $torchIndex
        torch = "torch==$torchVersion"
        torchvision = "torchvision==$torchvisionVersion"
    } | ConvertTo-Json -Compress
    exit 0
}

function Invoke-VisionGatePython {
    param([string[]]$Arguments)
    & $venvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

function Find-Python311 {
    $candidates = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidates += [pscustomobject]@{ File = $launcher.Source; Prefix = @("-3.11") }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $candidates += [pscustomobject]@{ File = $python.Source; Prefix = @() }
    }
    $localPython = Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe"
    if (Test-Path -LiteralPath $localPython) {
        $candidates += [pscustomobject]@{ File = $localPython; Prefix = @() }
    }
    foreach ($candidate in $candidates) {
        $probe = Invoke-NativeProbe { & $candidate.File @($candidate.Prefix) -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" }
        if ($probe.Succeeded) { return $candidate }
    }
    return $null
}

try {
    Set-Location -LiteralPath $root

    if ($Action -eq "Check") {
        if (-not (Test-Path -LiteralPath $venvPython)) { throw "VisionGate is not installed" }
        Invoke-VisionGatePython @("-c", "import cv2, fastapi, lap, torch, torchvision, ultralytics, uvicorn, zeroconf; from Crypto.Cipher import AES; print('VisionGate is ready. Runtime:', 'CUDA - ' + torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')")
        exit 0
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        $basePython = Find-Python311
        if (-not $basePython) {
            $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
            if (-not $winget) {
                throw "Python 3.11 is required and Windows Package Manager is unavailable. Install Python 3.11 from python.org, then run this installer again."
            }
            Write-Host "Installing Python 3.11..." -ForegroundColor Cyan
            & $winget.Source install --exact --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "Python installation failed with exit code $LASTEXITCODE" }
            $basePython = Find-Python311
            if (-not $basePython) { throw "Python installed, but Windows has not made it available yet. Restart Windows, then run the installer again." }
        }
        Write-Host "Creating VisionGate's private Python environment..." -ForegroundColor Cyan
        & $basePython.File @($basePython.Prefix) -m venv (Join-Path $root ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment" }
    }

    Write-Host "Preparing the installer..." -ForegroundColor Cyan
    Invoke-VisionGatePython @("-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip")

    $backendProbe = Invoke-NativeProbe { & $venvPython -c "import torch; print('cuda' if torch.version.cuda else 'cpu')" }
    $installedBackend = if ($backendProbe.Succeeded) { $backendProbe.Output | Select-Object -Last 1 } else { "" }
    $pipArguments = @("-m", "pip", "install", "--disable-pip-version-check")
    if ($Action -eq "Update") { $pipArguments += "--upgrade" }
    if ($installedBackend -and $installedBackend.Trim() -ne $backendName) {
        Write-Host "Switching PyTorch from $($installedBackend.Trim()) to $backendName..." -ForegroundColor Cyan
        $pipArguments += "--force-reinstall"
    }
    $pipArguments += @("torch==$torchVersion", "torchvision==$torchvisionVersion", "--index-url", $torchIndex)
    Write-Host "Installing the $backendName vision engine..." -ForegroundColor Cyan
    Invoke-VisionGatePython $pipArguments

    $dependencyArguments = @("-m", "pip", "install", "--disable-pip-version-check")
    if ($Action -eq "Update") { $dependencyArguments += "--upgrade" }
    $dependencyArguments += @("-r", (Join-Path $root "requirements.txt"))
    Write-Host "Installing VisionGate dependencies..." -ForegroundColor Cyan
    Invoke-VisionGatePython $dependencyArguments
    Invoke-VisionGatePython @("-m", "pip", "check")

    Write-Host "Downloading the vision model files if needed..." -ForegroundColor Cyan
    Invoke-VisionGatePython @("-c", "from ultralytics import YOLO; from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small; YOLO('yolo11s.pt'); mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.DEFAULT)")

    if (-not (Test-Path -LiteralPath (Join-Path $root ".env"))) {
        Copy-Item -LiteralPath (Join-Path $root ".env.example") -Destination (Join-Path $root ".env")
    }

    if ($Action -eq "Install") {
        try {
            $shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "VisionGate.lnk"
            $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
            $shortcut.TargetPath = Join-Path $root "Launch VisionGate.bat"
            $shortcut.WorkingDirectory = $root
            $shortcut.Description = "Launch VisionGate"
            $shortcut.Save()
        } catch {
            Write-Warning "VisionGate installed, but Windows did not allow a desktop shortcut. Use Launch VisionGate.bat instead."
        }
    }

    Invoke-VisionGatePython @("-c", "import torch; print('VisionGate installation complete. Runtime:', 'CUDA - ' + torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')")
    exit 0
} catch {
    Write-Host "" 
    Write-Host "VisionGate setup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
