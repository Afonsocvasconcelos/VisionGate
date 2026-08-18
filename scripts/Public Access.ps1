param(
    [ValidateSet("Configure", "Firewall", "Plan")]
    [string]$Action = "Configure",
    [string]$HostName = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"

function Get-PublicHost {
    param([string]$Value)
    $Value = $Value.Trim().ToLowerInvariant()
    $address = $null
    if ([Net.IPAddress]::TryParse($Value, [ref]$address)) {
        if ($address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { return $Value }
        throw "Enter an IPv4 address or domain name"
    }
    $pattern = "^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])$"
    if ($Value -match $pattern) { return $Value }
    throw "Enter only a public IPv4 address or domain name, for example 203.0.113.10 or door.example.com"
}

try {
    Set-Location -LiteralPath $root

    if ($Action -eq "Plan") {
        $HostName = Get-PublicHost $HostName
        [pscustomobject]@{
            public_url = "http://${HostName}:83"
            port = 83
            allowed_host = $HostName
        } | ConvertTo-Json -Compress
        exit 0
    }

    if ($Action -eq "Firewall") {
        & netsh advfirewall firewall delete rule name="VisionGate" *> $null
        & netsh advfirewall firewall delete rule name="VisionGate HTTP 83" *> $null
        & netsh advfirewall firewall delete rule name="VisionGate HTTPS" *> $null
        & netsh advfirewall firewall delete rule name="VisionGate HTTPS Setup" *> $null
        & netsh advfirewall firewall add rule name="VisionGate HTTP 83" dir=in action=allow protocol=TCP localport=83 profile=any
        if ($LASTEXITCODE -ne 0) { throw "Could not open Windows Firewall port 83" }
        exit 0
    }

    if (-not (Test-Path -LiteralPath $python)) {
        throw "Install VisionGate before configuring online access"
    }
    if (-not $HostName) { $HostName = Read-Host "Public IPv4 address or DDNS name" }
    $HostName = Get-PublicHost $HostName
    & $python -c "import sys; from pathlib import Path; from auth import _set_env_values; _set_env_values(Path('.env'), {'VISIONGATE_PUBLIC_HOST': sys.argv[1], 'VISIONGATE_ALLOWED_HOSTS': sys.argv[1], 'VISIONGATE_SECURE_COOKIES': '0'})" $HostName
    if ($LASTEXITCODE -ne 0) { throw "Could not save the public address" }

    $firewallArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action Firewall"
    $firewall = Start-Process powershell.exe -ArgumentList $firewallArguments -Verb RunAs -Wait -PassThru
    if ($firewall.ExitCode -ne 0) { throw "Windows Firewall access was not granted" }

    $lanAddress = & $python -c "from core import local_ipv4_addresses; a=local_ipv4_addresses(); print(a[0] if a else 'THIS_PC')"
    Write-Host ""
    Write-Host "Public URL: http://${HostName}:83" -ForegroundColor Green
    Write-Host "WARNING: HTTP does not encrypt passwords, video, sessions, or door commands." -ForegroundColor Yellow
    Write-Host "1. In the router, forward external TCP port 83 to $lanAddress port 83."
    Write-Host "2. Remove any old VisionGate forwards for ports 80, 443, and 8000."
    Write-Host "3. Restart VisionGate and test the URL using mobile data, not Wi-Fi."
    exit 0
} catch {
    Write-Host ""
    Write-Host "VisionGate online setup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
