# Start CLIProxyAPI gateway and the provider selector on user logon.
# ponytail: no service install; a logon task reuses the same idempotent scripts.
$ErrorActionPreference = 'Continue'
$script = 'D:\MY_DESIGN\Agent-router-Pro-Max\script\Start-Proxy.ps1'
if (-not (Test-Path -LiteralPath $script)) { exit 0 }
try {
    & $script | Out-Null
} catch {
    # Log nothing sensitive; the gateway writes its own logs under D:\MY_DESIGN\Agent-router-Pro-Max\logs.
    exit 1
}
exit 0