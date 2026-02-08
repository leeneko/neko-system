# 🐇 Rabbit System - 워커 프로그램 분리 가이드

## 개요

Rabbit System의 워커 프로그램이 다음과 같이 **분리**되었습니다:

| 워커 | 역할 | 실행 위치 | 기능 |
|------|------|----------|------|
| **combined_worker.py** | 크롤 워커 | 노트북 (크롤러) | 소설 다운로드, 본문 추출, OCI 업로드 |
| **translate_worker.py** | 번역 워커 | 사용자의 데스크탑 PC | 번역만 담당 (별도 처리, EXE 빌드 가능) |

---

## 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                     OCI Cloud (Database)                    │
│  ┌──────────────────────────────────────────────────────┐  │
│  │          PostgreSQL (rabbit_novel)                   │  │
│  │  ┌──────────────┐    ┌───────────────────────────┐  │  │
│  │  │   chapters   │    │ chapter_translations      │  │  │
│  │  │  (원본)      │    │ (번역본)                  │  │  │
│  │  └──────────────┘    └───────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
        ▲                           ▲
        │ (원본 저장)                │ (번역본 저장)
        │                           │
        │                   ┌───────┴────────────┐
        │                   │                    │
   ┌────┴─────────┐    ┌────────────────────────┴───┐
   │  combined_   │    │  translate_worker.py      │
   │  worker.py   │    │  (데스크탑 PC)             │
   │              │    │                            │
   │  역할:       │    │  역할:                     │
   │  - HTML 크롤 │    │  - 원본 읽기               │
   │  - 본문 추출 │    │  - 번역 수행               │
   │  - 원본 저장 │    │  - 번역본 저장             │
   └──────────────┘    └────────────────────────────┘
      (OCI서버)              (Desktop PC)
```

---

## ✨ 주요 개선사항

### 이전 방식 (combined_worker가 모든 것을 처리)
- ❌ 크롤 중에 번역이 동시에 진행되어 느림
- ❌ 번역 실패 시 전체 작업이 지연됨
- ❌ 리소스 중복 사용 (OCI와 PC 모두 GPU/CPU 필요)

### 새로운 방식 (역할 분리)
- ✅ **크롤 워커**: 빠르게 원본만 수집 (간단한 작업)
- ✅ **번역 워커**: 데스크탑에서 번역에만 집중 (고성능 머신 사용)
- ✅ **독립적 운영**: 각 워커가 독립적으로 작동 가능
- ✅ **확장성**: 필요한 만큼 번역 워커를 추가 가능

---

## 🚀 설정 방법

### 1️⃣ 역할 정리: 어디에서 무엇을 실행해야 하나요?

- **OCI (서버)**: FastAPI 웹서버와 PostgreSQL DB를 호스팅합니다. 개발·배포용 서버로 사용하며, 워커(노트북, 데스크탑)는 OCI의 API나 DB로 데이터를 업로드/저장합니다.
- **노트북 (크롤러)**: 실제 크롤링과 파싱을 수행합니다. 크롤 결과(원본 텍스트)는 OCI의 API(`/worker/submit`)로 업로드하거나 DB에 직접 저장합니다. 즉, 크롤러는 `combined_worker.py`를 노트북에서 실행하세요.
- **데스크탑 (번역기)**: 번역 전용 워커(`translate_worker.py`)를 실행하거나 `pyinstaller`로 EXE를 빌드하여 데스크탑에서 실행하세요. 데스크탑은 DB에 직접 접근해 번역 결과를 `chapter_translations`에 저장합니다.

   참고: OCI 서버는 Linux 환경에서 운영됩니다. EXE 빌드는 Windows 데스크탑에서 수행하고, 빌드된 `.exe`는 Windows에서 실행하세요.
---

### 2️⃣ 데스크탑 PC (translate_worker 실행 위치)

#### 2-1. 필수 설치 (노트북 & 데스크탑 공통)

```bash
# Python 3.8 이상 필요
python --version

# 워커 디렉토리로 이동
cd /home/ubuntu/workspace/rabbit-system/worker

# 의존성 설치
pip install -r requirements.txt

# 데스크탑에서 번역을 수행할 경우 (CPU 만 사용 시)
pip install torch --index-url https://download.pytorch.org/whl/cpu

# GPU가 있으면 GPU용 torch 설치 (CUDA 버전에 맞게)
# pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

#### 2-2. 환경변수 설정
```bash
# Windows (cmd.exe)
set DB_HOST=144.24.87.146
set DB_PORT=5432
set DB_USER=kcc_user
set DB_PASS=kcc_password
set DB_NAME=rabbit_novel
set TRANSLATE_MODEL=Helsinki-NLP/opus-mt-ja-ko
set MAX_CHARS=512
set WORKER_INTERVAL=2

# Linux/Mac (bash)
export DB_HOST=144.24.87.146
export DB_PORT=5432
export DB_USER=kcc_user
export DB_PASS=kcc_password
export DB_NAME=rabbit_novel
export TRANSLATE_MODEL=Helsinki-NLP/opus-mt-ja-ko
export MAX_CHARS=512
export WORKER_INTERVAL=2
```

#### 2-3. 번역 워커 실행 (데스크탑)

```bash
# 환경변수 설정 (예)
export DB_HOST=144.24.87.146
export DB_PORT=5432
export DB_USER=kcc_user
export DB_PASS=kcc_password
export DB_NAME=rabbit_novel
export TRANSLATE_MODEL=Helsinki-NLP/opus-mt-ja-ko
export MAX_CHARS=512

python translate_worker.py
```

#### 2-4. 번역 워커를 EXE로 빌드 (데스크탑 / Windows에서 빌드하세요)

빌드는 Windows 데스크탑에서 수행해야 하며, 빌드된 실행파일은 Windows에서 실행합니다. Linux(OCI)에서 Windows용 EXE를 만들려 하지 마세요 — 권장 방법은 실제 Windows 환경에서 `pyinstaller`를 실행하는 것입니다.

Windows (cmd.exe) 예시:

```powershell
> python -m venv .venv
> .\\.venv\\Scripts\\activate
> pip install -r requirements.txt
> pip install pyinstaller
> pyinstaller --onefile --name translate_worker translate_worker.py

# 결과: dist\\translate_worker.exe
```

PowerShell 예시:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --onefile --name translate_worker translate_worker.py

# 결과: .\\dist\\translate_worker.exe
```

참고: 만약 Windows 머신이 없고 Linux에서 빌드해야 한다면 `wine` 및 cross-compilation 도구를 사용할 수 있으나, 권장하지 않습니다. 가능하면 실제 Windows 환경에서 빌드하세요.

#### 2-4. 로그 확인
```
translate_error.log  # 에러 로그 (WARNING 이상만 기록)
```

---

## 🛠️ 환경변수 상세 설명

### combined_worker.py

| 환경변수 | 기본값 | 설명 |
|---------|--------|------|
| `OCI_SERVER_URL` | `http://144.24.87.146:8001` | OCI API 서버 주소 |
| `PROXY_LIST` | (없음) | 프록시 리스트 (`;`로 구분) |
| `DRISSION_PORT` | `9222` | Chrome 디버깅 포트 |
| `DRISSION_MANUAL_WAIT` | `600` | CAPTCHA 대기 시간(초) |
| `WORKER_BUFFER_SIZE` | `10` | 미리 로드할 작업 수 |

### translate_worker.py

| 환경변수 | 기본값 | 설명 |
|---------|--------|------|
| `DB_HOST` | `144.24.87.146` | PostgreSQL 호스트 |
| `DB_PORT` | `5432` | PostgreSQL 포트 |
| `DB_USER` | `kcc_user` | DB 사용자명 |
| `DB_PASS` | `kcc_password` | DB 비밀번호 |
| `DB_NAME` | `rabbit_novel` | DB 이름 |
| `TRANSLATE_MODEL` | `Helsinki-NLP/opus-mt-ja-ko` | 번역 모델 (일본어→한국어) |
| `MAX_CHARS` | `512` | 한 번에 번역할 최대 문자 수 |
| `WORKER_INTERVAL` | `2` | 작업 사이의 대기 시간(초) |

---

## 📝 번역 모델 선택

### 추천 모델
- **일본어 → 한국어**: `Helsinki-NLP/opus-mt-ja-ko` ⭐ (추천)
- **일본어 → 영어**: `Helsinki-NLP/opus-mt-ja-en`
- **중국어(간체) → 한국어**: `Helsinki-NLP/opus-mt-zh-ko`

### 모델 다운로드 미리 하기 (선택)
```bash
# 첫 실행 시 자동으로 다운로드되지만, 미리 준비하고 싶으면:
python -c "from transformers import pipeline; \
  pipeline('translation', model='Helsinki-NLP/opus-mt-ja-ko')"
```

---

## 🔍 동작 확인

### combined_worker가 제대로 작동하는지 확인
```
✅ TASK COMPLETED: 12345
   Title: 어떤 소설 제목
   Original text: 50000 chars
   (번역은 translate_worker.py에서 별도로 처리됨)
```

### translate_worker가 제대로 작동하는지 확인
```
📖 챕터 번역 시작: ID=12345
   제목: 어떤 소설 제목
   URL: https://ncode.syosetu.com/...
   원본 크기: 50000자
   모델: Helsinki-NLP/opus-mt-ja-ko

✓ 번역 완료: 45000자

✅ 챕터 번역 완료: ID=12345
   제목: 어떤 소설 제목
   번역 크기: 45000자
```

---

## 📊 성능 최적화

### translate_worker 성능 향상 팁

1. **GPU 사용**
   ```bash
   # NVIDIA GPU (CUDA 설치 필수)
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
   ```

2. **MAX_CHARS 조정**
   - 메모리 많음 → `1024` 이상으로 증가 (번역 속도 ↑)
   - 메모리 부족 → `256`으로 감소 (안정성 ↑)

3. **WORKER_INTERVAL 조정**
   - 빠른 처리 원함 → `0.5`로 감소
   - 서버 부하 줄임 → `5` 이상으로 증가

---

## 🐛 문제 해결

### combined_worker가 "번역은 translate_worker에서 처리" 메시지만 출력
✅ **정상 작동** - combined_worker는 이제 번역하지 않습니다.

### translate_worker가 "번역할 챕터 없음"이라는 메시지만 출력
- combined_worker가 원본을 아직 저장하지 않음 → combined_worker 실행 확인
- 이미 모든 챕터가 번역됨 → 새로운 소설 추가 필요

### "DB 연결 실패" 에러
```bash
# 1. DB 정보 확인
ping 144.24.87.146

# 2. 포트 확인
telnet 144.24.87.146 5432

# 3. 환경변수 확인
echo %DB_HOST%    # Windows
echo $DB_HOST     # Linux/Mac
```

### "transformers 미설치" 에러
```bash
pip install transformers torch

# 또는 requirements.txt로 한번에
pip install -r requirements.txt
```

---

## 📌 주의사항

1. **DB 접근 권한**
   - translate_worker는 **OCI DB에 직접 접근**합니다
   - DB 비밀번호는 환경변수로 보안 유지

2. **네트워크**
   - translate_worker가 OCI DB에 접근 가능한 네트워크 환경 필요
   - 방화벽/VPN 설정 확인

3. **모델 다운로드 시간**
   - 첫 실행 시 모델 다운로드에 시간 소요 (네트워크에 따라 5-30분)
   - 모델 저장 위치: `~/.cache/huggingface/transformers/`

---

## 📋 체크리스트

### OCI 서버 (combined_worker)
- [ ] combined_worker.py 수정됨 (번역 기능 제거)
- [ ] 기존 크롤링 시스템 정상 작동
- [ ] translate_worker와 독립적으로 실행 가능

### 데스크탑 PC (translate_worker)
- [ ] Python 3.8 이상 설치
- [ ] `pip install -r requirements.txt` 완료
- [ ] DB 환경변수 설정 완료
- [ ] 번역 모델 선택 완료
- [ ] translate_worker.py 실행 확인
- [ ] 로그 파일 생성 확인

---

## 📞 추가 지원

각 워커의 상세한 로그를 보려면:

```bash
# combined_worker (디버그 레벨)
export LOGLEVEL=DEBUG
python combined_worker.py

# translate_worker (디버그 레벨)
export LOGLEVEL=DEBUG
python translate_worker.py
```

이제 번역 성능 저하 없이 크롤링할 수 있습니다! 🚀
