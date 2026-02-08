"""
================================================================================
🐇 Rabbit System - Translation Worker (translate_worker.py)
================================================================================

목적:
  - 별도의 머신(데스크탑 PC)에서 실행되는 번역 전용 워커
  - OCI 클라우드의 PostgreSQL DB에 직접 접근
  - 원본 챕터가 있지만 번역이 없는 항목을 찾아 번역 수행
  - Hugging Face transformers를 사용한 고품질 번역 (로컬에서만 실행)
  - 번역 결과를 DB에 저장

작동 흐름:
  1. OCI DB에 연결 (psycopg2)
  2. 번역할 챕터 조회 (content는 있지만 translation이 없는 항목)
  3. 해당 챕터의 원본 가져오기
  4. Hugging Face pipeline을 사용해 번역 수행
  5. 번역 결과를 chapter_translations 테이블에 저장
  6. 다음 챕터로 계속 처리 (라운드-로빈 또는 순번)

환경변수:
  - DB_HOST: PostgreSQL 호스트 (예: 144.24.87.146)
  - DB_PORT: PostgreSQL 포트 (기본값: 5432)
  - DB_USER: 데이터베이스 사용자 (예: kcc_user)
  - DB_PASS: 데이터베이스 비밀번호
  - DB_NAME: 데이터베이스 이름 (예: rabbit_novel)
  - TRANSLATE_MODEL: 번역 모델 (예: Helsinki-NLP/opus-mt-ja-ko)
  - MAX_CHARS: 한 번에 번역할 최대 문자 수 (기본값: 512)
  - WORKER_INTERVAL: 작업 사이의 대기 시간(초) (기본값: 2)

DB 스키마:
  chapters:
    - id (PRIMARY KEY)
    - novel_id
    - url
    - title
    - content (원본 텍스트)
    - status
    - next_url
  
  chapter_translations:
    - id (PRIMARY KEY)
    - chapter_id (FK to chapters)
    - engine (번역 엔진 이름)
    - content (번역된 텍스트)
    - created_at

================================================================================
"""

import os
import sys
import time
import logging
import traceback
try:
    import psycopg2
    HAS_PSYCOPG2 = True
except Exception:
    HAS_PSYCOPG2 = False
    # In a bundled exe the module may be missing; provide a clear message and exit later if used
    def _missing_db_exit():
        msg = (
            "Required module 'psycopg2' not found.\n"
            "On Windows install the binary wheel package: pip install psycopg2-binary\n"
            "Then rebuild the EXE ensuring the build environment has the package installed.\n"
            "If you only want to run without DB access, set DB_HOST='' or run in an environment with psycopg2."
        )
        print(msg)
    # Do not exit immediately here to allow PyInstaller to bundle; we'll check HAS_PSYCOPG2 before DB use.
from typing import Optional, Dict, List

# ============================================================================
# 로깅 설정
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Add file handler to write logs to error.log
try:
    if getattr(sys, 'frozen', False):
        base_for_logs = os.path.dirname(sys.executable)
    else:
        base_for_logs = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

    log_file = os.path.join(base_for_logs, "translate_error.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(file_handler)
except Exception as e:
    print(f"Warning: Could not set up file logging: {e}")

def log_info(msg):
    """정보 로그 출력"""
    logging.info(msg)

def log_debug(msg):
    """디버그 로그 출력"""
    logging.debug(msg)

def log_error(msg):
    """에러 로그 출력"""
    logging.error(msg)

def log_exception(msg):
    """예외 로그 출력 (스택 트레이스 포함)"""
    logging.exception(msg)

# ============================================================================
# 전역 설정
# ============================================================================

# DB 연결 설정
DB_HOST = os.environ.get("DB_HOST", "144.24.87.146")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "kcc_user")
DB_PASS = os.environ.get("DB_PASS", "kcc_password")
DB_NAME = os.environ.get("DB_NAME", "rabbit_novel")

# 번역 설정
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "Helsinki-NLP/opus-mt-ja-ko")
MAX_CHARS = int(os.environ.get("MAX_CHARS", "512"))

# 워커 설정
WORKER_INTERVAL = int(os.environ.get("WORKER_INTERVAL", "2"))

# HuggingFace 캐시 디렉토리 설정
HF_HOME = os.environ.get("HF_HOME", os.path.join(os.path.expanduser("~"), ".cache", "huggingface"))
os.environ["HF_HOME"] = HF_HOME

# ============================================================================
# 번역 관련 설정 확인
# ============================================================================

try:
    from transformers import pipeline
    HAS_TRANSFORMERS = True
    log_info("✓ transformers 설치됨 (번역 기능 활성화)")
except Exception as e:
    # Catch broad exceptions because importing transformers may raise OSError from torch (DLL load failures)
    HAS_TRANSFORMERS = False
    log_error(f"❌ transformers or torch import error: {e}")
    log_error("If running as an EXE, common fixes are:\n"
              " - Build the EXE on Windows with a CPU-only torch wheel: `pip install torch --index-url https://download.pytorch.org/whl/cpu`\n"
              " - Ensure Microsoft Visual C++ Redistributable is installed (2015-2019/2017).\n"
              " - If you need CUDA, install matching CUDA runtime/drivers on target machine.\n"
              "After fixing the build environment, rebuild the EXE and retry.")

# ============================================================================
# DB 연결 함수
# ============================================================================

def get_db_connection() -> Optional[psycopg2.extensions.connection]:
    """PostgreSQL DB에 연결"""
    if not HAS_PSYCOPG2:
        log_error("psycopg2 not installed in this environment. Cannot connect to DB.")
        log_error("Install psycopg2-binary and rebuild the EXE, or run in a Python env with psycopg2.")
        return None
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASS,
            dbname=DB_NAME,
            connect_timeout=10
        )
        return conn
    except psycopg2.Error as e:
        log_error(f"DB 연결 실패: {e}")
        log_exception("DB connection error")
        return None

# ============================================================================
# 번역할 챕터 조회
# ============================================================================

def get_chapter_to_translate(conn: psycopg2.extensions.connection) -> Optional[Dict]:
    """
    번역할 다음 챕터를 조회
    
    조건:
    - content가 있고 비어있지 않음
    - chapter_translations에 이 챕터의 번역이 없음 (같은 engine)
    
    반환:
    - {"id": chapter_id, "url": url, "title": title, "content": content}
    - 없으면 None
    """
    if not conn:
        return None
    
    try:
        cur = conn.cursor()
        
        # 원본만 있고 번역이 없는 챕터 찾기 (일본어 소설만)
        cur.execute(f"""
            SELECT c.id, c.url, c.title, c.content
            FROM chapters c
            JOIN novels n ON c.novel_id = n.id
            WHERE n.source_language = 'ja'
              AND c.content IS NOT NULL 
              AND c.content != ''
              AND NOT EXISTS (
                  SELECT 1 FROM chapter_translations ct
                  WHERE ct.chapter_id = c.id 
                    AND ct.engine = %s
              )
            ORDER BY c.id ASC
            LIMIT 1
        """, (TRANSLATE_MODEL,))
        
        row = cur.fetchone()
        cur.close()
        
        if row:
            return {
                "id": row[0],
                "url": row[1],
                "title": row[2],
                "content": row[3]
            }
        
        return None
    except Exception as e:
        log_error(f"챕터 조회 실패: {e}")
        log_exception("get_chapter_to_translate error")
        return None

# ============================================================================
# 번역 함수
# ============================================================================

def translate_text(text: str) -> Optional[str]:
    """
    Hugging Face transformers를 사용해 텍스트 번역
    
    MAX_CHARS 크기로 청크를 나누어 번역 (메모리 부하 감소)
    
    Args:
        text: 번역할 텍스트
    
    Returns:
        번역된 텍스트, 또는 실패 시 None
    """
    if not HAS_TRANSFORMERS:
        log_error("transformers 미설치")
        return None
    
    if not text or not text.strip():
        log_error("빈 텍스트 전달됨")
        return None
    
    try:
        log_info(f"🔄 번역 시작: 모델={TRANSLATE_MODEL}")
        
        # 모델 로드
        try:
            log_info(f"   모델 로딩중: {TRANSLATE_MODEL}...")
            log_info(f"   캐시 디렉토리: {HF_HOME}")
            
            # CPU 사용으로 설정 (device=-1)
            translator = pipeline(
                "translation", 
                model=TRANSLATE_MODEL,
                device=-1,  # CPU 사용
                model_kwargs={"low_cpu_mem_usage": True}
            )
            log_info(f"   ✓ 모델 로딩 완료")
        except Exception as e:
            log_error(f"   ❌ 모델 로딩 실패: {e}")
            log_error(f"   💡 해결 방법:")
            log_error(f"      1. 네트워크 연결 확인")
            log_error(f"      2. HuggingFace 모델 다운로드: https://huggingface.co/Helsinki-NLP/opus-mt-ja-ko")
            log_error(f"      3. 캐시에 저장된 모델이 있는지 확인: {HF_HOME}")
            log_exception("Model loading error")
            return None
        
        # 청크 단위로 번역
        parts = [text[i:i+MAX_CHARS] for i in range(0, len(text), MAX_CHARS)]
        log_info(f"   {len(parts)}개 청크로 분할 (최대 {MAX_CHARS}자)")
        
        translated_parts = []
        for chunk_idx, chunk in enumerate(parts, 1):
            try:
                log_info(f"   청크 {chunk_idx}/{len(parts)} 번역중 ({len(chunk)}자)...")
                
                # 번역 수행
                result = translator(chunk)
                
                # 결과 추출
                translated_text = ""
                if isinstance(result, list) and len(result) > 0:
                    first = result[0]
                    if isinstance(first, dict):
                        if 'translation_text' in first:
                            translated_text = first['translation_text']
                        elif 'label' in first:
                            translated_text = first.get('label', '')
                        else:
                            translated_text = str(first)
                    else:
                        translated_text = str(first)
                elif isinstance(result, str):
                    translated_text = result
                else:
                    translated_text = str(result)
                
                if translated_text:
                    translated_parts.append(translated_text)
                    log_info(f"   ✓ 청크 {chunk_idx} 완료: {len(translated_text)}자")
                else:
                    log_error(f"   ⚠️ 청크 {chunk_idx} 결과 없음")
            
            except Exception as e:
                log_error(f"   ❌ 청크 {chunk_idx} 번역 실패: {e}")
                log_exception(f"Chunk {chunk_idx} translation error")
                # 계속 진행
        
        # 번역 결과 합치기
        if translated_parts:
            translated = "\n\n".join(translated_parts)
            log_info(f"✓ 번역 완료: {len(translated)}자")
            return translated
        else:
            log_error("❌ 번역 결과가 비어있음")
            return None
    
    except Exception as e:
        log_error(f"❌ 번역 중 예외 발생: {e}")
        log_exception("Translation error")
        return None

# ============================================================================
# DB에 번역 저장
# ============================================================================

def save_translation(conn: psycopg2.extensions.connection, 
                    chapter_id: int, 
                    translated_content: str) -> bool:
    """
    번역 결과를 DB에 저장
    
    chapter_translations 테이블에 INSERT 또는 UPDATE
    """
    if not conn:
        return False
    
    try:
        cur = conn.cursor()
        
        # INSERT OR UPDATE (ON CONFLICT clause)
        cur.execute("""
            INSERT INTO chapter_translations (chapter_id, engine, content)
            VALUES (%s, %s, %s)
            ON CONFLICT (chapter_id, engine) DO UPDATE 
            SET content = EXCLUDED.content
        """, (chapter_id, TRANSLATE_MODEL, translated_content))
        
        conn.commit()
        cur.close()
        
        log_info(f"✓ DB에 번역 저장: chapter_id={chapter_id}, engine={TRANSLATE_MODEL}")
        return True
    
    except Exception as e:
        log_error(f"❌ DB 저장 실패: {e}")
        log_exception("save_translation error")
        try:
            conn.rollback()
        except Exception:
            pass
        return False

# ============================================================================
# 주요 처리 함수
# ============================================================================

def process_one_chapter(conn: psycopg2.extensions.connection) -> bool:
    """
    하나의 챕터를 번역하고 저장
    
    Returns:
        성공 시 True, 아무것도 할 것이 없거나 실패 시 False
    """
    # 번역할 챕터 조회
    chapter = get_chapter_to_translate(conn)
    if not chapter:
        log_info("⏸️ 번역할 챕터 없음")
        return False
    
    chapter_id = chapter["id"]
    title = chapter.get("title", "알 수 없음")
    url = chapter.get("url", "")
    content = chapter["content"]
    
    log_info(f"\n" + "="*70)
    log_info(f"📖 챕터 번역 시작: ID={chapter_id}")
    log_info(f"   제목: {title}")
    log_info(f"   URL: {url}")
    log_info(f"   원본 크기: {len(content)}자")
    log_info(f"   모델: {TRANSLATE_MODEL}")
    log_info(f"="*70)
    
    # 번역 수행
    translated_content = translate_text(content)
    if not translated_content:
        log_error(f"❌ 챕터 {chapter_id} 번역 실패")
        return False
    
    # DB에 보관
    if not save_translation(conn, chapter_id, translated_content):
        log_error(f"❌ 챕터 {chapter_id} 저장 실패")
        return False
    
    log_info(f"\n" + "="*70)
    log_info(f"✅ 챕터 번역 완료: ID={chapter_id}")
    log_info(f"   제목: {title}")
    log_info(f"   번역 크기: {len(translated_content)}자")
    log_info(f"="*70 + "\n")
    
    return True

def main():
    """메인 루프"""
    log_info("="*70)
    log_info("🐇 Rabbit System - Translation Worker 시작")
    log_info("="*70)
    log_info(f"DB_HOST: {DB_HOST}:{DB_PORT}")
    log_info(f"DB_NAME: {DB_NAME}")
    log_info(f"TRANSLATE_MODEL: {TRANSLATE_MODEL}")
    log_info(f"MAX_CHARS: {MAX_CHARS}")
    
    if not HAS_TRANSFORMERS:
        log_error("❌ transformers 미설치 - 종료")
        return
    
    # DB 연결
    conn = get_db_connection()
    if not conn:
        log_error("❌ DB 연결 실패 - 종료")
        return
    
    log_info("✓ DB 연결 성공")
    
    try:
        iteration = 0
        while True:
            iteration += 1
            log_debug(f"[반복 {iteration}] 번역할 챕터 조회...")
            
            try:
                # 하나의 챕터 처리
                success = process_one_chapter(conn)
                
                if not success:
                    # 번역할 것이 없으면 대기
                    log_info(f"⏳ {WORKER_INTERVAL}초 대기 후 재시도...")
                    time.sleep(WORKER_INTERVAL)
                else:
                    # 처리 완료 후 짧은 대기
                    wait_time = max(1, WORKER_INTERVAL // 2)
                    log_info(f"⏳ {wait_time}초 대기 후 다음 챕터 처리...")
                    time.sleep(wait_time)
            
            except KeyboardInterrupt:
                log_info("⌨️ 사용자 중단 요청 - 종료")
                break
            
            except Exception as e:
                log_error(f"⚠️ 처리 중 오류 발생: {e}")
                log_exception("Main loop error")
                log_info(f"⏳ {WORKER_INTERVAL}초 대기 후 재시도...")
                time.sleep(WORKER_INTERVAL)
                
                # DB 연결 상태 확인
                try:
                    if conn.closed:
                        log_info("🔄 DB 재연결중...")
                        conn = get_db_connection()
                        if not conn:
                            log_error("❌ DB 재연결 실패")
                            time.sleep(5)
                except Exception:
                    log_info("🔄 DB 재연결중...")
                    conn = get_db_connection()
                    if not conn:
                        log_error("❌ DB 재연결 실패")
                        time.sleep(5)
    
    finally:
        if conn:
            try:
                conn.close()
                log_info("✓ DB 연결 종료")
            except Exception:
                pass
        log_info("="*70)
        log_info("🐇 Translation Worker 종료")
        log_info("="*70)

if __name__ == "__main__":
    main()
