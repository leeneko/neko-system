# run_translate.ps1 - PowerShell runner for translate_worker.exe
# Edit environment variables below as needed

$env:DB_HOST = "144.24.87.146"
$env:DB_PORT = "5432"
$env:DB_USER = "kcc_user"
$env:DB_PASS = "kcc_password"
$env:DB_NAME = "rabbit_novel"
$env:TRANSLATE_MODEL = "Helsinki-NLP/opus-mt-ja-ko"
$env:MAX_CHARS = "512"

$exe1 = Join-Path $PSScriptRoot "dist\translate_worker.exe"
$exe2 = Join-Path $PSScriptRoot "dist\translate_worker\translate_worker.exe"

if (Test-Path $exe1) {
    & $exe1 @args
} elseif (Test-Path $exe2) {
    & $exe2 @args
} else {
    Write-Error "translate_worker.exe not found in dist\ or dist\translate_worker\.`nBuild using pyinstaller and place the result in the dist folder."
}
