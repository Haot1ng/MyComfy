# Open the two MyComfy test links from a single Windows PowerShell window.
[CmdletBinding()]
param(
    [switch]$SingleHost,
    [string]$Config,
    [string]$Distro,
    [string]$Repo
)

$ErrorActionPreference = 'Stop'

function Invoke-CoreConfig {
    # Ask WSL to switch the running core to the requested origin, or restore it with 'off'.
    param([string]$Mode)
    $arguments = @('-d', $Distro, '--cd', $Repo, 'python3', 'scripts/deploy.py')
    if ($SingleHost) { $arguments += '--single-host' }
    if ($Config) { $arguments += @('--config', $Config) }
    $result = & wsl.exe @arguments --test-tunnel $Mode --tunnel-json
    if ($LASTEXITCODE -ne 0) { throw "WSL could not apply the '$Mode' core configuration." }
    return (ConvertFrom-Json -InputObject ($result -join [Environment]::NewLine))
}

function Test-PortsInUse {
    # Tailscale stores persistent rules at the root and temporary rules under Foreground.
    param($Rules)
    if (!$Rules) { return $false }
    if ($Rules.TCP.'443' -or $Rules.TCP.'8443') { return $true }
    foreach ($entry in $Rules.Foreground.PSObject.Properties) {
        if (Test-PortsInUse $entry.Value) { return $true }
    }
    return $false
}

function Test-LocalPage {
    # Check the same Windows loopback endpoint the native Funnel client will forward to.
    param([int]$Port)
    $url = "http://127.0.0.1:$Port/"
    $request = [System.Net.WebRequest]::Create($url)
    $request.Proxy = $null
    $request.Timeout = 5000
    $request.Method = 'HEAD'
    try { $request.GetResponse().Close() }
    catch { throw "Windows cannot reach MyComfy at $url. Check Docker Desktop and the core service." }
}

# The script normally lives in the WSL checkout. Use its UNC path to locate the matching distro.
if ($PSScriptRoot -match '^\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)\\(.+)\\scripts$') {
    if (!$Distro) { $Distro = $Matches[1] }
    if (!$Repo) { $Repo = '/' + $Matches[2].Replace('\', '/') }
}
if (!$Distro -or !$Repo) { throw 'Run this script from the WSL checkout, or supply -Distro and -Repo.' }

# winget can finish before the current Windows terminal picks up the updated PATH.
$tailscale = (Get-Command tailscale.exe -CommandType Application -ErrorAction SilentlyContinue).Source
if (!$tailscale) {
    $tailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (!(Test-Path -LiteralPath $tailscale -PathType Leaf)) {
        throw 'Install Tailscale on Windows, open it and sign in, then run this command again.'
    }
}
$status = & $tailscale status --json
if ($LASTEXITCODE -ne 0) { throw 'Open Tailscale on Windows and sign in, then try again.' }
$status = ConvertFrom-Json -InputObject ($status -join [Environment]::NewLine)
if ($status.BackendState -ne 'Running' -or !$status.Self.DNSName) {
    throw 'Open Tailscale on Windows and sign in, then try again.'
}
$deviceName = $status.Self.DNSName.TrimEnd('.')

# Keep another copy of this launcher from restoring core while this session is running.
$mutex = [System.Threading.Mutex]::new($false, 'Local\MyComfyTestFunnel')
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (!$locked) { throw 'Another MyComfy tunnel window is already running.' }
    $rules = & $tailscale funnel status --json
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the current Windows Tailscale rules.' }
    if (Test-PortsInUse (ConvertFrom-Json -InputObject ($rules -join [Environment]::NewLine))) {
        throw 'Tailscale is already using HTTPS port 443 or 8443. Check the existing session first.'
    }

    try {
        # Record cleanup before configuring core: Compose can fail after replacing its container.
        Write-Host 'Preparing MyComfy test links...'
        $ports = Invoke-CoreConfig $deviceName
        Test-LocalPage $ports.public_port
        Test-LocalPage $ports.admin_port
        & (Join-Path $PSScriptRoot 'funnel-session.ps1') -TailscalePath $tailscale -DeviceName $deviceName `
            -PublicPort $ports.public_port -AdminPort $ports.admin_port
    } finally {
        # The native helper closes its own two listeners before this restores the WSL service.
        [void](Invoke-CoreConfig 'off')
        Write-Host 'Restored the original MyComfy configuration.'
    }
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
