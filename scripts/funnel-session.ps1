# Run two temporary HTTPS listeners until Ctrl+C or either native CLI process exits.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$DeviceName,
    [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$PublicPort,
    [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$AdminPort,
    [string]$TailscalePath = 'tailscale.exe'
)

$ErrorActionPreference = 'Stop'
$children = New-Object 'System.Collections.Generic.List[System.Diagnostics.Process]'

function Assert-FunnelsRunning {
    # Check owned processes while the other listener starts and during the test session.
    param([System.Diagnostics.Process]$Starting)

    foreach ($child in $children) {
        if ($child -ne $Starting -and $child.HasExited) {
            throw "Tailscale Funnel exited (code $($child.ExitCode)). See the CLI message above."
        }
    }
}

function Start-ForegroundFunnel {
    # Confirm this process has successfully registered its public URL before starting another.
    param([int]$HttpsPort, [int]$LocalPort, [string]$Origin)

    $child = New-Object System.Diagnostics.Process
    $child.StartInfo.FileName = $TailscalePath
    $child.StartInfo.Arguments = "funnel --https=$HttpsPort http://127.0.0.1:$LocalPort"
    $child.StartInfo.UseShellExecute = $false
    $child.StartInfo.CreateNoWindow = $true
    $child.StartInfo.RedirectStandardOutput = $true
    # Leave stderr attached so authentication, port conflicts and network failures are visible.
    $children.Add($child)
    [void]$child.Start()

    $expectPublicUrl = $false
    while ($true) {
        # Poll an async read so Ctrl+C and a sibling exiting also work during browser approval.
        $read = $child.StandardOutput.ReadLineAsync()
        while (!$read.IsCompleted) {
            Assert-FunnelsRunning -Starting $child
            Start-Sleep -Milliseconds 150
        }
        $line = $read.GetAwaiter().GetResult()
        if ($null -eq $line) {
            throw "Funnel for HTTPS port $HttpsPort closed before startup. If approval was requested, complete it and run the script again."
        }
        $line = $line.Trim()
        if ($line -eq 'Available on the internet:') {
            # The CLI emits this banner only after the daemon accepts this child's configuration.
            $expectPublicUrl = $true
        } elseif ($expectPublicUrl -and $line) {
            if ($line.TrimEnd('/') -ne $Origin) {
                throw "Tailscale returned '$line' for HTTPS port $HttpsPort. Expected '$Origin/'."
            }
            Assert-FunnelsRunning
            # The live CLI stays connected to keep the listener open. Drain its remaining banner.
            [void]$child.StandardOutput.ReadToEndAsync()
            return
        } elseif ($line -match 'https?://') {
            Write-Host "Tailscale setup: $line"
        }
    }
}

$publicOrigin = 'https://' + $DeviceName.TrimEnd('.')
$adminOrigin = $publicOrigin + ':8443'
try {
    # The CLI reads and updates shared daemon state. Register the two listeners sequentially.
    Start-ForegroundFunnel -HttpsPort 443 -LocalPort $PublicPort -Origin $publicOrigin
    Start-ForegroundFunnel -HttpsPort 8443 -LocalPort $AdminPort -Origin $adminOrigin
    Write-Host "Workbench: $publicOrigin/"
    Write-Host "Admin:     $adminOrigin/"
    Write-Host 'Press Ctrl+C to close both tunnels.'
    while ($true) {
        Assert-FunnelsRunning
        Start-Sleep -Milliseconds 250
    }
} finally {
    # Closing these specific native clients drops their daemon sessions, including on Ctrl+C.
    foreach ($child in $children) {
        try {
            if (!$child.HasExited) {
                $child.Kill()
                $child.WaitForExit()
            }
        } catch [System.InvalidOperationException] {
            # A failed start or simultaneous exit leaves no live process to stop.
        } finally {
            $child.Dispose()
        }
    }
}
