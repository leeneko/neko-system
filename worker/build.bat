@echo off
setlocal

REM Optional: activate venv before running this.

py -m pip install --upgrade pip
py -m pip install -r requirements.txt

REM Install PyTorch (CPU-only). This avoids CUDA DLL dependency issues.
py -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

REM Build single EXE without console window
py -m pip install pyinstaller
py -m PyInstaller --onefile --noconsole ^
  --name RabbitWorker ^
  --collect-all transformers ^
  --collect-all sentencepiece ^
  --collect-all torch ^
  --collect-binaries torch ^
  combined_worker.py

echo Done. EXE is in dist\RabbitWorker.exe
endlocal
