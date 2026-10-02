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
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onedir --windowed --name Prism ^
  --icon design\icons\prism.ico ^
  --version-file version_info.txt ^
  --add-data "static;static" ^
  --add-data "design\icons;design\icons" ^
  --exclude-module unittest ^
  --exclude-module pydoc ^
  --exclude-module pdb ^
  --exclude-module doctest ^
  --exclude-module test ^
  --exclude-module tkinter ^
  --exclude-module turtle ^
  --exclude-module curses ^
  --exclude-module idlelib ^
  --exclude-module pydoc_data ^
  --exclude-module setuptools ^
  --exclude-module pkg_resources ^
  --exclude-module multiprocessing ^
  --exclude-module PIL.ImageTk ^
  --exclude-module PIL.ImageShow ^
  --exclude-module PIL.ImageQt ^
  --exclude-module PIL.AvifImagePlugin ^
  --exclude-module PIL.WebPImagePlugin ^
  --exclude-module PIL.PdfImagePlugin ^
  --exclude-module PIL.TiffImagePlugin ^
  --exclude-module PIL.IptcImagePlugin ^
  --exclude-module PIL.McIdasImagePlugin ^
  --exclude-module PIL.MpegImagePlugin ^
  --exclude-module PIL.FpxImagePlugin ^
  --exclude-module PIL.DcxImagePlugin ^
  --exclude-module PIL.BufrStubImagePlugin ^
  --exclude-module PIL.GribStubImagePlugin ^
  --exclude-module PIL.Hdf5StubImagePlugin ^
  --exclude-module PIL.SpiderImagePlugin ^
  --exclude-module PIL.CurImagePlugin ^
  --exclude-module PIL.EpsImagePlugin ^
  --exclude-module PIL.FitsImagePlugin ^
  --exclude-module PIL.FliImagePlugin ^
  --exclude-module PIL.GbrImagePlugin ^
  --exclude-module PIL.IcnsImagePlugin ^
  --exclude-module PIL.ImImagePlugin ^
  --exclude-module PIL.ImtImagePlugin ^
  --exclude-module PIL.MpoImagePlugin ^
  --exclude-module PIL.PcdImagePlugin ^
  --exclude-module PIL.PcxImagePlugin ^
  --exclude-module PIL.PixarImagePlugin ^
  --exclude-module PIL.PpmImagePlugin ^
  --exclude-module PIL.PsdImagePlugin ^
  --exclude-module PIL.SgiImagePlugin ^
  --exclude-module PIL.SunImagePlugin ^
  --exclude-module PIL.TgaImagePlugin ^
  --exclude-module PIL.WmfImagePlugin ^
  --exclude-module PIL.XbmImagePlugin ^
  --exclude-module PIL.XpmImagePlugin ^
  --exclude-module PIL.XVThumbImagePlugin ^
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
