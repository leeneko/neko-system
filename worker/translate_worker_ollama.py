"""
================================================================================
🐇 Rabbit System - Local AI Translation Worker (Ollama Edition)
================================================================================

목적:
  - 비용 0원으로 무제한 대량 번역 수행
  - 로컬에 설치된 Ollama(Llama3, Gemma2 등)를 활용
  - OCI DB에서 원본을 가져와 번역 후 저장

필수조건:
  1. Ollama 설치 (https://ollama.com)
  2. 모델 다운로드 (cmd: ollama pull gemma2)
  3. Ollama 실행 중이어야 함

================================================================================
"""

import os
import sys
import time
import logging
import requests
import json

try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    print("❌ psycopg2 모듈이 없습니다. pip install psycopg2-binary")

# ============================================================================
# 설정
# ============================================================================

# DB 연결 설정 (환경에 맞게 수정하세요)
DB_HOST = os.environ.get("DB_HOST", "144.24.87.146")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASS = os.environ.get("DB_PASS", "neko15746+")
DB_NAME = os.environ.get("DB_NAME", "rabbit_novel")

# Ollama 설정
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gemma3:12b"  # 또는 'llama3', 'gemma2:2b' 등 설치한 모델명
TRANSLATION_ENGINE = f"ollama-{MODEL_NAME}" # DB에 저장될 엔진 이름

# 긴 텍스트 분할 설정 (AI 문맥 길이 제한 방지)
MAX_CHUNK_SIZE = 1000 

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

def log_info(msg): logging.info(msg)
def log_error(msg): logging.error(msg)

# ============================================================================
# DB 기능
# ============================================================================

def get_db_connection():
    if not HAS_PSYCOPG2: return None
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, dbname=DB_NAME,
            connect_timeout=10
        )
        conn.set_session(autocommit=True)
        return conn
    except Exception as e:
        log_error(f"DB 연결 실패: {e}")
        return None

def get_chapter_to_translate(conn):
    """번역할 챕터 조회 (아직 번역 안 된 것)"""
    try:
        cur = conn.cursor()
        # novels 테이블과 조인하여 일본어(ja) 소설만 가져오는 것이 좋음
        # 현재는 우선 모든 챕터 중 번역이 없는 것을 가져오도록 설정
        query = """
            SELECT c.id, c.title, c.content
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
        """
        try:
            cur.execute(query, (TRANSLATION_ENGINE,))
            row = cur.fetchone()
            cur.close()
        except Exception as e:
            # 일부 환경에는 novels.source_language 컬럼이 없을 수 있음
            if "source_language" in str(e):
                cur.close()
                log_error("novels.source_language 컬럼이 없어 필터를 제거합니다.")
                cur = conn.cursor()
                fallback_query = """
                    SELECT c.id, c.title, c.content
                    FROM chapters c
                    WHERE c.content IS NOT NULL 
                      AND c.content != ''
                      AND NOT EXISTS (
                          SELECT 1 FROM chapter_translations ct
                          WHERE ct.chapter_id = c.id 
                            AND ct.engine = %s
                      )
                    ORDER BY c.id ASC
                    LIMIT 1
                """
                cur.execute(fallback_query, (TRANSLATION_ENGINE,))
                row = cur.fetchone()
                cur.close()
            else:
                raise
        
        if row:
            return {"id": row[0], "title": row[1], "content": row[2]}
        return None
    except Exception as e:
        log_error(f"챕터 조회 실패: {e}")
        return None

def save_translation(conn, chapter_id, content):
    """DB에 번역 저장"""
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO chapter_translations (chapter_id, engine, content, created_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (chapter_id, engine) 
            DO UPDATE SET content = EXCLUDED.content, created_at = NOW()
        """, (chapter_id, TRANSLATION_ENGINE, content))
        cur.close()
        return True
    except Exception as e:
        log_error(f"DB 저장 실패: {e}")
        return False

# ============================================================================
# Ollama 번역 로직
# ============================================================================

def call_ollama(text):
    """Ollama API 호출"""
    
    # 프롬프트 엔지니어링: 번역 품질을 결정하는 핵심
    prompt = f"""
    Role: Professional Web Novel Translator
    Task: Translate the following Japanese text to Korean.
    Guidelines:
    1. Output ONLY the translated Korean text. Do not include notes or explanations.
    2. Maintain the formatting (line breaks) of the original text.
    3. Use a natural style suitable for Korean web novels.
    
    [Japanese Text]:
    {text}
    
    [Korean Translation]:
    """

    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False, # 한 번에 받기
        "options": {
            "temperature": 0.3, # 창의성 낮춤 (정확한 번역 위해)
            "num_ctx": 4096     # 문맥 길이
        }
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=300) # PC 성능에 따라 오래 걸릴 수 있음
        if response.status_code == 200:
            result = response.json()
            return result.get("response", "").strip()
        else:
            log_error(f"Ollama 오류: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        log_error(f"Ollama 연결 실패: {e}")
        log_error("Ollama가 실행 중인지 확인하세요. (cmd: ollama serve)")
        return None

def split_text(text, max_length):
    """긴 텍스트를 문단 단위로 자르기"""
    parts = []
    current_part = ""
    
    for paragraph in text.split('\n'):
        if len(current_part) + len(paragraph) > max_length:
            parts.append(current_part)
            current_part = paragraph + "\n"
        else:
            current_part += paragraph + "\n"
    
    if current_part:
        parts.append(current_part)
    return parts

def translate_text(full_text):
    """전체 텍스트 번역 (분할 처리 포함)"""
    if not full_text: return None
    
    # 텍스트가 너무 길면 나눠서 번역
    chunks = split_text(full_text, MAX_CHUNK_SIZE)
    translated_chunks = []
    
    log_info(f"   -> {len(chunks)}개 파트로 나누어 번역 진행...")
    
    for i, chunk in enumerate(chunks):
        if not chunk.strip(): 
            translated_chunks.append("")
            continue
            
        log_info(f"      파트 {i+1}/{len(chunks)} 번역 중 ({len(chunk)}자)...")
        translated = call_ollama(chunk)
        
        if translated:
            translated_chunks.append(translated)
        else:
            log_error(f"      파트 {i+1} 번역 실패. 중단.")
            return None
            
    return "\n".join(translated_chunks)

# ============================================================================
# 메인 루프
# ============================================================================

def main():
    log_info("="*60)
    log_info(f"🐇 Rabbit System - Local AI 번역기 ({MODEL_NAME})")
    log_info("   비용: 무료 / 제한: 무제한 / 속도: PC 성능 의존")
    log_info("="*60)
    
    conn = get_db_connection()
    if not conn: return

    while True:
        try:
            # 1. 대상 조회
            chapter = get_chapter_to_translate(conn)
            if not chapter:
                log_info("💤 번역할 챕터가 없습니다. 10초 대기...")
                time.sleep(10)
                continue
            
            cid = chapter['id']
            title = chapter['title']
            content = chapter['content']
            
            log_info(f"▶ [ID:{cid}] '{title}' 번역 시작 ({len(content)}자)")
            
            # 2. 로컬 AI 번역
            start_time = time.time()
            translated_content = translate_text(content)
            duration = time.time() - start_time
            
            if translated_content:
                # 3. 저장
                if save_translation(conn, cid, translated_content):
                    log_info(f"✅ 완료: [ID:{cid}] (소요시간: {duration:.1f}초)")
                else:
                    log_error("❌ DB 저장 실패")
            else:
                log_error("❌ 번역 실패 (Ollama 응답 없음)")
                time.sleep(5) # 오류 시 잠시 대기

        except KeyboardInterrupt:
            log_info("종료 요청 받음.")
            break
        except Exception as e:
            log_error(f"⚠️ 시스템 오류: {e}")
            time.sleep(5)
            # DB 연결 끊김 등 대비하여 재연결 로직 추가 가능

    if conn: conn.close()

if __name__ == "__main__":
    main()
