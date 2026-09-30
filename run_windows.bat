@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치 후 다시 실행하세요.
  echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [처음 실행] 가상환경을 만듭니다...
  python -m venv .venv || goto :err
)
".venv\Scripts\python.exe" -c "import beeplabel" >nul 2>nul
if errorlevel 1 (
  echo [처음 실행] 설치합니다. 1~2분 걸립니다...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install . || goto :err
)

rem  다른 PC에서도 접속하게 하려면: run_windows.bat lan
set HOST=127.0.0.1
if /i "%1"=="lan" set HOST=0.0.0.0

".venv\Scripts\python.exe" -m beeplabel serve --host %HOST%
pause
exit /b 0

:err
echo 설치 중 오류가 발생했습니다.
pause
exit /b 1
