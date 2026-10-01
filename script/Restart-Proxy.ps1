#requires -Version 5.1
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Pause-Proxy.ps1')
& (Join-Path $PSScriptRoot 'Start-Proxy.ps1')
