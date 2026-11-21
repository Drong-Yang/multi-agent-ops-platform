@echo off
setlocal
pushd "%~dp0"
if /i not "%~1"=="-Live" (
    echo Add -Live to run the explicitly authorized cloud voice acceptance.
    popd
    exit /b 2
)
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright" set "NODE_PATH=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules;%NODE_PATH%"
set "DELIVERY_EXIT=0"
node scripts\live_voice_browser.cjs --run > .runtime\delivery-voice-run.log 2>&1
if "%~2"=="-VoiceOnly" (
    set "DELIVERY_EXIT=%ERRORLEVEL%"
    goto :complete
)
if errorlevel 1 set "DELIVERY_EXIT=1"
backend\.venv\Scripts\python.exe scripts\run_tests.py
if errorlevel 1 set "DELIVERY_EXIT=1"
node --test scripts\managed-processes.test.cjs scripts\windows-powershell.test.cjs scripts\callback-gateway.test.cjs
if errorlevel 1 set "DELIVERY_EXIT=1"
pushd frontend-ops
call npm run build
if errorlevel 1 set "DELIVERY_EXIT=1"
popd
:complete
echo Delivery verification exit code: %DELIVERY_EXIT%
popd
exit /b %DELIVERY_EXIT%
