#requires -Version 5.1
$script=Join-Path $PSScriptRoot 'route_selector.py'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script) } | ForEach-Object { Stop-Process -Id $_.ProcessId }
Write-Output 'Provider selector stopped. Gateway unchanged.'
