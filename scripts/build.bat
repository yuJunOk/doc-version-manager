@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."

echo ========================================
echo   DocVM - build exe
echo ========================================
echo.

REM ---- resolve PYTHON ----
if defined PYTHON goto :check_python

if exist "C:\Users\13758\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" (
  set "PYTHON=C:\Users\13758\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
  goto :check_python
)
if exist "C:\Users\13758\.workbuddy-ai\binaries\python\envs\default\python.exe" (
  set "PYTHON=C:\Users\13758\.workbuddy-ai\binaries\python\envs\default\python.exe"
  goto :check_python
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  goto :check_python
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  goto :check_python
)

REM skip WindowsApps stub: where may find it but it cannot run
for /f "delims=" %%P in ('where python 2^>nul') do (
  echo %%P | find /i "WindowsApps" >nul
  if errorlevel 1 (
    set "PYTHON=%%P"
    goto :check_python
  )
)

goto :ask_python

:check_python
"%PYTHON%" -V >nul 2>&1
if errorlevel 1 (
  echo.
  echo WARNING: "%PYTHON%" cannot run.
  set "PYTHON="
  goto :ask_python
)
goto :have_python

:ask_python
echo.
echo Python not found automatically.
echo Please paste full path to python.exe, then press Enter.
echo Example: C:\Users\13758\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe
echo.
set /p "PYTHON=PYTHON= "
if not defined PYTHON (
  echo Empty path.
  goto :fail
)
REM strip surrounding quotes if user pasted with quotes
set "PYTHON=!PYTHON:"=!"
if not exist "!PYTHON!" (
  echo File not found: !PYTHON!
  goto :ask_again
)
"!PYTHON!" -V >nul 2>&1
if errorlevel 1 (
  echo Cannot run: !PYTHON!
  goto :ask_again
)
goto :have_python

:ask_again
echo.
set "PYTHON="
goto :ask_python

:have_python
echo Using: %PYTHON%
"%PYTHON%" -V
echo.

echo [1/3] Install dependencies...
if /i "%SKIP_PIP%"=="1" (
  echo SKIP_PIP=1, skip install.
  goto :after_pip
)

REM Prefer Aliyun mirror (official PyPI often hits SSL errors in CN)
"%PYTHON%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
if errorlevel 1 (
  echo.
  echo Aliyun failed, retry with Tsinghua mirror...
  "%PYTHON%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
)
if errorlevel 1 (
  echo.
  echo Mirror failed, retry official PyPI...
  "%PYTHON%" -m pip install -r requirements.txt
)
if errorlevel 1 (
  echo.
  echo pip install failed. Network/SSL issue.
  echo Fix network, or install manually then re-run with:
  echo   set SKIP_PIP=1
  echo   scripts\build.bat
  goto :fail
)
:after_pip

echo.
echo [2/3] Clean old build files...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [3/3] PyInstaller (scripts\DocVersionManager.spec)...
"%PYTHON%" -m PyInstaller --noconfirm --distpath dist --workpath build "scripts\DocVersionManager.spec"
if errorlevel 1 (
  echo.
  echo PyInstaller failed.
  goto :fail
)

echo.
echo ========================================
if exist "dist\DocVersionManager.exe" (
  echo  OK: dist\DocVersionManager.exe
  echo  Double-click to run. No Python install needed.
) else (
  echo  FAIL: dist\DocVersionManager.exe not found
  goto :fail
)
echo ========================================
echo.
pause
exit /b 0

:fail
echo.
echo Build failed. Check messages above.
pause
exit /b 1
