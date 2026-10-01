#requires -Version 5.1
param([switch]$Open, [switch]$SkipGatewayStart)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if(-not $SkipGatewayStart){ & (Join-Path $PSScriptRoot 'Start-Proxy.ps1') }
$script=Join-Path $PSScriptRoot 'route_selector.py'
$python=(& python -c 'import sys; print(sys.executable)').Trim()
if(-not (Test-Path -LiteralPath $python -PathType Leaf)){throw 'Python runtime not found.'}
$existing=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script) })
$listener=@(Get-NetTCPConnection -LocalPort 8318 -State Listen -ErrorAction SilentlyContinue)
if($listener.Count -and ($existing.Count -ne 1 -or $listener[0].OwningProcess -ne $existing[0].ProcessId)){throw 'Port 8318 belongs to another process.'}
if(-not $existing.Count){
 $process=Start-Process -FilePath $python -ArgumentList @('"'+$script+'"') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'logs\selector-stdout.log') -RedirectStandardError (Join-Path $root 'logs\selector-stderr.log') -PassThru
 $deadline=(Get-Date).AddSeconds(15)
 do{
  if($process.HasExited){throw 'Selector exited; inspect logs.'}
  $listener=@(Get-NetTCPConnection -LocalPort 8318 -State Listen -ErrorAction SilentlyContinue | Where-Object OwningProcess -eq $process.Id)
  if($listener.Count){break}
  Start-Sleep -Milliseconds 200
 }while((Get-Date) -lt $deadline)
 if(-not $listener.Count){$process|Stop-Process;throw 'Selector did not start.'}
}
Write-Output 'Provider selector: http://127.0.0.1:8318/'
if($Open){Start-Process 'http://127.0.0.1:8318/'}
