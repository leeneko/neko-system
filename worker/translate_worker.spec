# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['translate_worker.py'],
    pathex=[],
    binaries=[('C:\\Workspace\\neko-system\\worker\\.venv\\Lib\\site-packages\\torch\\lib\\c10.dll', 'torch\\lib')],
    datas=[],
    hiddenimports=['psycopg2', 'psycopg2._psycopg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='translate_worker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='translate_worker',
)
