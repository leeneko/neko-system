@echo off
REM run_translate.bat - sets env vars and runs translate_worker.exe (onefile or onedir)

REM --- Edit these values as needed ---
set DB_HOST=144.24.87.146
set DB_PORT=5432
set DB_USER=kcc_user
set DB_PASS=kcc_password
set DB_NAME=rabbit_novel
set TRANSLATE_MODEL=Helsinki-NLP/opus-mt-ja-ko
set MAX_CHARS=512

REM --- Try to run onefile then onedir locations ---
if exist "%~dp0dist\translate_worker.exe" (
    "%~dp0dist\translate_worker.exe" %*
    goto :eof
)

if exist "%~dp0dist\translate_worker\translate_worker.exe" (
    "%~dp0dist\translate_worker\translate_worker.exe" %*
    goto :eof
)

echo ERROR: translate_worker.exe not found in dist\ or dist\translate_worker\
echo Build the EXE using pyinstaller and place it in the dist folder next to this script.
pause
