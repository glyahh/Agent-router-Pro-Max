#requires -Version 5.1
# Start this installation only; repeated calls do not create another instance.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root 'cli-proxy-api.exe'
$config = Join-Path $root 'config.yaml'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "Missing executable: $exe" }
$match = [regex]::Match([IO.File]::ReadAllText($config), '(?m)^port:[ \t]*(\d+)[ \t]*(?:#.*)?\r?$')
if (-not $match.Success) { throw 'config.yaml must contain a numeric top-level port.' }
$port = [int]$match.Groups[1].Value
if ($port -lt 1 -or $port -gt 65535) { throw 'Invalid port in config.yaml.' }
$existing = @(Get-Process -Name 'cli-proxy-api' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
if ($existing.Count -gt 1) { throw 'Multiple instances found. Run Pause-Proxy.ps1 first.' }
$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    if (-not $existing.Count -or $listener.OwningProcess -ne $existing[0].Id) {
        throw "Port $port is occupied by another process. Nothing was stopped."
    }
}
$created = $false
if ($existing.Count) {
    $proxy = $existing[0]
} else {
    $logs = Join-Path $root 'logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $proxy = Start-Process -FilePath $exe -ArgumentList @('-config', ('"' + $config + '"')) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs "$stamp-stdout.log") -RedirectStandardError (Join-Path $logs "$stamp-stderr.log") -PassThru
    $created = $true
}
$deadline = (Get-Date).AddSeconds(20)
do {
    $proxy.Refresh()
    if ($proxy.HasExited) { throw "CLIProxyAPI exited. Check $root\logs." }
    $ready = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq $proxy.Id })
    if ($ready.Count) {
        Write-Output "Running: PID $($proxy.Id), port $port"
        & (Join-Path $PSScriptRoot 'Start-Selector.ps1') -SkipGatewayStart
        return
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)
if ($created -and -not $proxy.HasExited) { $proxy | Stop-Process -ErrorAction SilentlyContinue }
throw "CLIProxyAPI did not listen on port $port within 20 seconds."
