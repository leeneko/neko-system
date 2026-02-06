#!/usr/bin/env python3
"""빠른 구문 검사 스크립트"""

import py_compile
import sys

try:
    py_compile.compile('/home/ubuntu/workspace/rabbit-system/worker/combined_worker.py', doraise=True)
    print("✅ combined_worker.py 구문 검증 성공!")
    print("✨ 파일이 실행 가능한 상태입니다.")
    sys.exit(0)
except py_compile.PyCompileError as e:
    print(f"❌ 구문 오류 발견:\n{e}")
    sys.exit(1)
