<#
把 app\dist\Prism\ 里已经打好的包切到项目根（Prism.exe + _internal\）。

为什么单开一个脚本，而不是直接用 app\build.bat：
  build.bat 的第 2 步是「先 del Prism.exe、再 rmdir /s /q _internal，然后把 dist 里的搬过来」。
  **Prism 正在运行的时候跑它，del 会因为文件被占用而失败，但 rmdir 会把正在运行的那个
  程序的依赖删掉** —— 界面当场崩，而且旧包也回不来了。
  这个脚本在动手之前先确认没有 Prism 在跑，并把旧包整体留一份。

用法：
    powershell -ExecutionPolicy Bypass -File script\Switch-Build.ps1
#>
[CmdletBinding()]
param(
  [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root 'app\dist\Prism'
$NewExe = Join-Path $Dist 'Prism.exe'
$NewInt = Join-Path $Dist '_internal'
$OldExe = Join-Path $Root 'Prism.exe'
$OldInt = Join-Path $Root '_internal'

function Say($m) { Write-Host $m }

Say "项目根: $Root"

# ── 1. 前置检查 ────────────────────────────────────────────────────────────
if (-not (Test-Path $NewExe)) { throw "找不到新包 $NewExe。先在 app\ 里跑 pyinstaller（或 build.bat 的第 1 步）。" }
if (-not (Test-Path $NewInt)) { throw "找不到新包的 $NewInt。" }

# 按**可执行文件路径精确匹配**找 Prism，不用名字模糊匹配：这台机器上还有 DSH 的 node.exe
# 等一堆别的进程，历史上用 'My_Agent_Proxy' 这类正则误判过。
$running = @(Get-CimInstance Win32_Process -Filter "Name='Prism.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, 'OrdinalIgnoreCase') })
if ($running.Count -gt 0 -and -not $Force) {
  $pids = ($running | ForEach-Object { $_.ProcessId }) -join ', '
  throw @"
Prism 正在运行（PID $pids，路径在 $Root 下），拒绝切换。

现在切会把正在运行的程序的 _internal\ 删掉，界面会当场崩，旧包也回不来。
请先**从托盘菜单正常退出** Prism（不是强杀），然后重跑本脚本。

（网关 cli-proxy-api.exe 不用管，它只读 config.yaml，不受影响。）
"@
}

# ── 2. 源 ↔ 包一致性（DEV-RULES C3）────────────────────────────────────────
Say ''
Say '核对源 ↔ 包 SHA256:'
$files = @('app.js', 'app.css', 'index.html',
           'pages\home.js', 'pages\usage.js', 'pages\monitor.js', 'pages\logs.js', 'pages\settings.js')
$bad = @()
foreach ($n in $files) {
  $a = Join-Path $Root "app\static\$n"
  $b = Join-Path $NewInt "static\$n"
  if (-not (Test-Path $a)) { $bad += "$n（源里没有）"; continue }
  if (-not (Test-Path $b)) { $bad += "$n（包里没有）"; continue }
  if ((Get-FileHash $a -Algorithm SHA256).Hash -ne (Get-FileHash $b -Algorithm SHA256).Hash) { $bad += $n }
}
if ($bad.Count) { throw "源与包不一致，拒绝切换：`n  " + ($bad -join "`n  ") }
Say "  8 个静态文件全部一致"

# 指纹：打包态的 APP_DIR = 项目根，用的是**根目录**那份 lock
$lockPath = Join-Path $Root 'route_selector.lock'
if (Test-Path $lockPath) {
  $lock = Get-Content $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $actual = (Get-FileHash (Join-Path $Root 'script\route_selector.py') -Algorithm SHA256).Hash.ToLower()
  if ($lock.sha256 -ne $actual) {
    throw @"
打包态指纹不匹配，拒绝切换。

  根 route_selector.lock : $($lock.sha256)
  script\route_selector.py: $actual

打包时 APP_DIR = 项目根，读的就是**根目录**那一份 lock。先按 DEV-RULES D5
重录两份指纹（只录 app\ 那份的话，双击 exe 会报"与首次运行时的版本不一致"）。
"@
  }
  Say "  打包态指纹一致（$($actual.Substring(0,16))...）"
} else {
  Say "  注意：没有根 route_selector.lock，首次启动会按当前文件重建"
}

# ── 3+4. 留档旧包 + 切换。**任一步失败都把现场还原** ────────────────────────
# 原实现是"先把旧包搬走、再把新包搬进来"，两步之间一失败（文件被占用 / 磁盘满 /
# 杀软锁住 / 中断）项目根就**既没有 Prism.exe 也没有 _internal**，只剩留档目录等人手工搬回
# （审查 HI-08）。新包存在与否第 1 步已经查过，所以这里只补真正缺的那一件：
# 失败时把搬进根的新包撤掉、把留档搬回来。
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$bk = Join-Path $Root "backups\agent-package-$stamp"
New-Item -ItemType Directory -Path $bk -Force | Out-Null
$moved = @()      # 从项目根搬去留档的（回滚要搬回来）
$placed = @()     # 从新包搬进项目根的（回滚要移走）
try {
  if (Test-Path $OldExe) {
    Move-Item $OldExe (Join-Path $bk 'Prism.exe')
    $moved += @{ from = (Join-Path $bk 'Prism.exe'); to = $OldExe }
  }
  if (Test-Path $OldInt) {
    Move-Item $OldInt (Join-Path $bk '_internal')
    $moved += @{ from = (Join-Path $bk '_internal'); to = $OldInt }
  }
  # **先登记再移动**：跨卷的 Move-Item 其实是"复制 + 删源"，中途失败会在目标留下半成品。
  # 若把登记写在 Move 之后，那一刻 $placed 里没有它，catch 就会跳过、留下半份 _internal
  # 而旧包还滞留在留档目录（审查 NB-04）。登记在前，catch 就能无条件把目标删掉再还原。
  $placed += $OldExe
  Move-Item $NewExe $OldExe
  $placed += $OldInt
  Move-Item $NewInt $OldInt
} catch {
  foreach ($p in $placed) { if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue } }
  foreach ($m in $moved) {
    if ((Test-Path $m.from) -and -not (Test-Path $m.to)) {
      Move-Item $m.from $m.to -ErrorAction SilentlyContinue
    }
  }
  throw "切换失败（$($_.Exception.Message)）。已尝试把旧包搬回原位，留档在 $bk。"
}
Say ''
Say "旧包已留档: $bk（万一要回滚就把它搬回来）"
Say "已切换: $OldExe"

# ── 5. 清理构建中间产物 ────────────────────────────────────────────────────
foreach ($p in (Join-Path $Root 'app\dist'), (Join-Path $Root 'app\build'), (Join-Path $Root 'app\Prism.spec')) {
  if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}
Say '已清理 app\dist / app\build / app\Prism.spec'

Say ''
Say '完成。双击 Prism.exe 即可（它会自己拉起 8317 的网关）。'
Say '要回滚：把上面那个 backups\agent-package-* 里的 Prism.exe 与 _internal 搬回项目根。'
