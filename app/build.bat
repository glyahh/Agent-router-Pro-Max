@echo off
REM Prism 打包脚本 —— 双击即可重新打包。
REM 产物：项目根目录下的 Prism.exe + _internal\（会自动从 app\dist 移过去）
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo [错误] 找不到 .venv\Scripts\pyinstaller.exe
  echo        先重建虚拟环境：
  echo          python -m venv .venv
  echo          .venv\Scripts\python.exe -m pip install -r ..\requirements.lock
  exit /b 1
)

echo [1/2] 打包中...
".venv\Scripts\pyinstaller.exe" --noconfirm --onedir --windowed --name Prism ^
  --icon design\icons\prism.ico ^
  --version-file version_info.txt ^
  --add-data "static;static" ^
  --add-data "design\icons;design\icons" ^
  --exclude-module PIL.AvifImagePlugin ^
  main.py
if errorlevel 1 (
  echo [错误] 打包失败
  exit /b 1
)

echo [2/2] 归位到项目根...
set "ROOT=%~dp0.."
if exist "dist\Prism\Prism.exe" (
  if exist "%ROOT%\Prism.exe" del /f /q "%ROOT%\Prism.exe"
  if exist "%ROOT%\_internal" rmdir /s /q "%ROOT%\_internal"
  move /y "dist\Prism\Prism.exe" "%ROOT%\" >nul
  move /y "dist\Prism\_internal" "%ROOT%\" >nul
  rmdir /s /q build dist
  del /f /q Prism.spec
  echo 完成：%ROOT%\Prism.exe
) else (
  echo [警告] 没找到 dist\Prism\Prism.exe，手工检查 app\dist
)
endlocal
