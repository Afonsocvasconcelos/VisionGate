param(
    [ValidateSet("Install", "Update", "Check", "Plan", "Backup", "SourceUpdate")]
    [string]$Action = "Install",
    [ValidateSet("Auto", "CPU", "CUDA")]
    [string]$Backend = "Auto"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$torchVersion = "2.12.1"
$torchvisionVersion = "0.27.1"
$appFiles = @(
    "app.py", "auth.py", "automation.py", "core.py", "enrollment.py",
    "ewelink_cloud.py", "ewelink_devices.py"
)

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

if ($Action -in @("Install", "Update", "Plan") -and $Backend -eq "Auto" -and $env:VISIONGATE_BACKEND -in @("CPU", "CUDA")) {
    $Backend = $env:VISIONGATE_BACKEND
}
if ($Action -in @("Install", "Update", "Plan") -and $Backend -eq "Auto") {
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

    if ($Action -eq "Backup") {
        $envFile = Join-Path $root ".env"
        $dataDirectory = Join-Path $root "data"
        if (-not (Test-Path -LiteralPath $envFile) -and -not (Test-Path -LiteralPath $dataDirectory)) {
            Write-Host "No runtime data needs a pre-update backup."
            exit 0
        }
        $destination = Join-Path (Join-Path $root "backups") ("pre-update-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff"))
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        if (Test-Path -LiteralPath $envFile) {
            Copy-Item -LiteralPath $envFile -Destination $destination
        }
        if (Test-Path -LiteralPath $dataDirectory) {
            Copy-Item -LiteralPath $dataDirectory -Destination $destination -Recurse
            $sourceDatabase = Join-Path $dataDirectory "whitelist.db"
            $backupDatabase = Join-Path (Join-Path $destination "data") "whitelist.db"
            if ((Test-Path -LiteralPath $sourceDatabase) -and (Test-Path -LiteralPath $venvPython)) {
                Remove-Item -LiteralPath $backupDatabase -Force -ErrorAction SilentlyContinue
                & $venvPython -c "import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()" $sourceDatabase $backupDatabase
                if ($LASTEXITCODE -ne 0) { throw "Could not create a consistent SQLite backup" }
            }
        }
        Write-Host "Pre-update backup saved to $destination" -ForegroundColor Green
        exit 0
    }

    if ($Action -eq "SourceUpdate") {
        if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) {
            Write-Host "Standalone copy detected; application files were not downloaded."
            exit 0
        }
        $git = Get-Command git.exe -ErrorAction SilentlyContinue
        $gitPath = if ($git) { $git.Source } else { $null }
        $gitCandidates = @(
            (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Git\cmd\git.exe"),
            (Join-Path $env:LocalAppData "Programs\Git\cmd\git.exe")
        )
        if (-not $gitPath) {
            $gitPath = $gitCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        }
        if (-not $gitPath) {
            $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
            if (-not $winget) { throw "Git is required for application updates and Windows Package Manager is unavailable" }
            Write-Host "Installing Git for future VisionGate updates..." -ForegroundColor Cyan
            & $winget.Source install --exact --id Git.Git --scope user --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "Git installation failed with exit code $LASTEXITCODE" }
            $gitPath = $gitCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
            if (-not $gitPath) { throw "Git installed, but Windows has not made it available yet. Restart Windows, then run the updater again." }
        }
        Write-Host "Downloading VisionGate application updates..." -ForegroundColor Cyan
        & $gitPath -C $root pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "Application update failed with exit code $LASTEXITCODE" }
        exit 0
    }

    if ($Action -eq "Check") {
        if (-not (Test-Path -LiteralPath $venvPython)) { throw "VisionGate is not installed" }
        Invoke-VisionGatePython @("-m", "pip", "check")
        Invoke-VisionGatePython @("-c", "import sys; from importlib.util import find_spec; required=('auth','cv2','fastapi','lap','torch','torchvision','ultralytics','uvicorn','zeroconf','Crypto.Cipher'); missing=[name for name in required if find_spec(name) is None]; sys.exit('Missing packages: ' + ', '.join(missing)) if missing else print('VisionGate is ready.')")
        Invoke-VisionGatePython (@("-m", "py_compile") + $appFiles)
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
    Invoke-VisionGatePython @("-c", "from ultralytics import YOLO; from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small; YOLO('yolo11n.pt'); mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.DEFAULT)")
    Invoke-VisionGatePython (@("-m", "py_compile") + $appFiles)

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
