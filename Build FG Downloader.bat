@echo off
title Building FG Downloader
cd /d "%~dp0"
rmdir /s /q build >nul 2>nul
echo Building FG Downloader - this takes 2-3 minutes.
echo.
where python >nul 2>nul || (where py >nul 2>nul && (set PY=py) || (echo Python is not installed. Get it from python.org and tick "Add Python to PATH". & pause & exit /b))
if not defined PY set PY=python
%PY% -m pip install -r requirements.txt || (echo Install failed. & pause & exit /b)
if not exist icon.ico %PY% make_icon.py
%PY% -m PyInstaller --noconfirm --onefile --windowed --name "FG Downloader" --icon icon.ico --collect-all curl_cffi --collect-submodules webview.platforms.winforms --collect-submodules webview.platforms.edgechromium --collect-data webview --exclude-module torch --exclude-module torchvision --exclude-module scipy --exclude-module pandas --exclude-module numpy --exclude-module matplotlib --exclude-module PIL --exclude-module pytest --exclude-module jinja2 --exclude-module IPython --exclude-module notebook --exclude-module tensorflow --exclude-module sklearn --exclude-module cv2 --exclude-module pygments --exclude-module sympy fgdl_gui.py || (echo Build failed. & pause & exit /b)
copy /y "dist\FG Downloader.exe" "%~dp0FG Downloader.exe" >nul
rmdir /s /q build >nul 2>nul
rmdir /s /q dist >nul 2>nul
del /q "FG Downloader.spec" >nul 2>nul

rem Setup file for sharing (needs Inno Setup 6: winget install JRSoftware.InnoSetup)
set ISCC=
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do if exist %%P set ISCC=%%P
if defined ISCC (
    %ISCC% /Q installer.iss || (echo Setup build failed. & pause & exit /b)
    echo.
    echo Done! Share "FG Downloader Setup.exe" with your friends.
) else (
    echo.
    echo Done! Built "FG Downloader.exe". Install Inno Setup 6 to also get "FG Downloader Setup.exe".
)
pause
