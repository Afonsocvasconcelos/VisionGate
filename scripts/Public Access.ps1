param(
    [ValidateSet("Configure", "Start", "Firewall", "Plan", "Update")]
    [string]$Action = "Configure",
    [string]$Domain = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$caddyfile = Join-Path $root "data\Caddyfile"

function Get-PublicDomain {
    param([string]$Value)
    $Value = $Value.Trim().ToLowerInvariant()
    $pattern = "^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])$"
    if ($Value -notmatch $pattern) {
        throw "Enter only a public domain name, for example door.example.com"
    }
    return $Value
}

function Get-CaddyConfig {
    param([string]$HostName)
    return "$HostName {`n`tencode zstd gzip`n`trequest_body {`n`t`tmax_size 2MB`n`t}`n`treverse_proxy 127.0.0.1:8000 {`n`t`tflush_interval -1`n`t}`n}`n"
}

function Find-Caddy {
    $command = Get-Command caddy.exe -ErrorAction SilentlyContinue
    $candidates = @()
    if ($command) { $candidates += $command.Source }
    $candidates += (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\caddy.exe")
    return $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

try {
    Set-Location -LiteralPath $root

    if ($Action -eq "Plan") {
        $Domain = Get-PublicDomain $Domain
        [pscustomobject]@{
            public_url = "https://$Domain"
            caddyfile = Get-CaddyConfig $Domain
        } | ConvertTo-Json -Compress
        exit 0
    }

    if ($Action -eq "Firewall") {
        & netsh advfirewall firewall delete rule name="VisionGate HTTPS" *> $null
        & netsh advfirewall firewall delete rule name="VisionGate HTTPS Setup" *> $null
        & netsh advfirewall firewall add rule name="VisionGate HTTPS" dir=in action=allow protocol=TCP localport=443 profile=any
        if ($LASTEXITCODE -ne 0) { throw "Could not open Windows Firewall port 443" }
        & netsh advfirewall firewall add rule name="VisionGate HTTPS Setup" dir=in action=allow protocol=TCP localport=80 profile=any
        if ($LASTEXITCODE -ne 0) { throw "Could not open Windows Firewall port 80" }
        exit 0
    }

    if ($Action -eq "Start") {
        if (-not (Test-Path -LiteralPath $caddyfile)) { exit 0 }
        $caddy = Find-Caddy
        if (-not $caddy) { throw "Caddy is missing; run Configure Online Access.bat" }
        & $caddy validate --config $caddyfile --adapter caddyfile *> $null
        if ($LASTEXITCODE -ne 0) { throw "The public HTTPS configuration is invalid" }
        & $caddy reload --config $caddyfile --adapter caddyfile *> $null
        if ($LASTEXITCODE -ne 0) {
            & $caddy start --config $caddyfile --adapter caddyfile *> $null
            if ($LASTEXITCODE -ne 0) { throw "Caddy could not start on ports 80 and 443" }
        }
        exit 0
    }

    if ($Action -eq "Update") {
        if (-not (Test-Path -LiteralPath $caddyfile)) { exit 0 }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw "Windows Package Manager is required to update Caddy" }
        & $winget.Source upgrade --exact --id CaddyServer.Caddy --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -notin @(0, -1978335189)) { throw "Caddy update failed with exit code $LASTEXITCODE" }
        exit 0
    }

    if (-not (Test-Path -LiteralPath $python)) {
        throw "Install VisionGate before configuring online access"
    }
    if (-not $Domain) { $Domain = Read-Host "Public domain or DDNS name (example: mydoor.duckdns.org)" }
    $Domain = Get-PublicDomain $Domain
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw "Windows Package Manager is required to install Caddy" }

    if (-not (Find-Caddy)) {
        Write-Host "Installing the Caddy HTTPS proxy..." -ForegroundColor Cyan
        & $winget.Source install --exact --id CaddyServer.Caddy --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "Caddy installation failed with exit code $LASTEXITCODE" }
    }
    if (-not (Find-Caddy)) { throw "Caddy installed, but Windows has not made it available yet. Restart Windows, then run this setup again." }

    New-Item -ItemType Directory -Force -Path (Split-Path $caddyfile) | Out-Null
    Set-Content -LiteralPath $caddyfile -Value (Get-CaddyConfig $Domain) -Encoding UTF8
    & $python -c "import sys; from pathlib import Path; from auth import _set_env_values; _set_env_values(Path('.env'), {'VISIONGATE_PUBLIC_HOST': sys.argv[1], 'VISIONGATE_ALLOWED_HOSTS': sys.argv[1], 'VISIONGATE_SECURE_COOKIES': '1'})" $Domain
    if ($LASTEXITCODE -ne 0) { throw "Could not save the public domain" }

    $firewallArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action Firewall"
    $firewall = Start-Process powershell.exe -ArgumentList $firewallArguments -Verb RunAs -Wait -PassThru
    if ($firewall.ExitCode -ne 0) { throw "Windows Firewall access was not granted" }

    $lanAddress = & $python -c "from core import local_ipv4_addresses; a=local_ipv4_addresses(); print(a[0] if a else 'THIS_PC')"
    Write-Host ""
    Write-Host "Public URL: https://$Domain" -ForegroundColor Green
    Write-Host "1. Point the domain's DNS A record to your public internet IP."
    Write-Host "2. In the router, forward TCP ports 80 and 443 to $lanAddress."
    Write-Host "3. Never forward port 8000; it remains for the private network only."
    Write-Host "4. Restart VisionGate and test the URL using mobile data, not Wi-Fi."
    exit 0
} catch {
    Write-Host ""
    Write-Host "VisionGate online setup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
