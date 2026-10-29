@echo off
title Building FF Downloader
cd /d "%~dp0"
rmdir /s /q build >nul 2>nul
echo Building FF Downloader - this takes 2-3 minutes, only needed once.
echo.
where python >nul 2>nul || (where py >nul 2>nul && (set PY=py) || (echo Python is not installed. Get it from python.org and tick "Add Python to PATH". & pause & exit /b))
if not defined PY set PY=python
%PY% -m pip install -r requirements.txt || (echo Install failed. & pause & exit /b)
%PY% -m PyInstaller --noconfirm --onefile --windowed --name "FF Downloader" --collect-all curl_cffi --collect-submodules webview.platforms.winforms --collect-submodules webview.platforms.edgechromium --collect-data webview --exclude-module torch --exclude-module torchvision --exclude-module scipy --exclude-module pandas --exclude-module numpy --exclude-module matplotlib --exclude-module PIL --exclude-module pytest --exclude-module jinja2 --exclude-module IPython --exclude-module notebook --exclude-module tensorflow --exclude-module sklearn --exclude-module cv2 --exclude-module pygments --exclude-module sympy ffdl_gui.py || (echo Build failed. & pause & exit /b)
copy /y "dist\FF Downloader.exe" "%~dp0FF Downloader.exe" >nul
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\FF Downloader.lnk');$s.TargetPath='%~dp0FF Downloader.exe';$s.WorkingDirectory='%~dp0';$s.Save()"
rmdir /s /q build >nul 2>nul
rmdir /s /q dist >nul 2>nul
del /q "FF Downloader.spec" >nul 2>nul
echo.
echo Done! "FF Downloader" is on your desktop. Opening it now...
start "" "%~dp0FF Downloader.exe"
timeout /t 4 >nul
