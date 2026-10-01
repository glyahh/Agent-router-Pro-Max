#requires -Version 5.1
# Pause means stop, not suspend. Configuration and credentials are kept.
# In-flight requests are interrupted. Only this installation is stopped.
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Stop-Selector.ps1')
$exe = Join-Path (Split-Path -Parent $PSScriptRoot) 'cli-proxy-api.exe'
$targets = @(Get-Process -Name 'cli-proxy-api' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
if (-not $targets.Count) {
    Write-Output 'Already paused.'
    return
}
foreach ($proxy in $targets) {
    $proxy.Refresh()
    if ($proxy.HasExited) { continue }
    $processId = $proxy.Id
    $proxy | Stop-Process -ErrorAction Stop
    if (-not $proxy.WaitForExit(10000)) { throw "Process $processId did not stop." }
    Write-Output "Paused: PID $processId. Configuration and credentials kept."
}
