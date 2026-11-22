@echo off
rem run-me-first.cmd -- the one thing to run before anything else (Windows).
rem
rem Builds the server's data folder from the two things you own: your FFBEWW APK and your client's data
rem folder. No Python, no openssl, no source checkout.
rem
rem   run-me-first.cmd [FFBEWW.apk] [client-data] [data folder]
setlocal
set HERE=%~dp0
set ENGINE=%HERE%engine\ffbe-engine.exe
if not exist "%ENGINE%" set ENGINE=%HERE%engine\ffbe-engine
if not exist "%ENGINE%" (
  echo error: engine not found beside this script -- unpack the whole archive first 1>&2
  exit /b 1
)
set APK=%~1
set CLIENT=%~2
set ROOT=%~3
if "%ROOT%"=="" set ROOT=%HERE%ffbe-data
if "%APK%"=="" set /p APK=Path to your FFBEWW APK: 
if "%CLIENT%"=="" set /p CLIENT=Path to your client data folder: 
"%ENGINE%" --prepare --apk "%APK%" --client-data "%CLIENT%" --data "%ROOT%"
if errorlevel 1 exit /b %errorlevel%
echo.
echo Prepared. Next: "%HERE%ffbe-server-app.exe" start --data "%ROOT%"
endlocal
