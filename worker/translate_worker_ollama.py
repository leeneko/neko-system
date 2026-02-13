"""
Rabbit System - Local AI Translation Worker (Ollama)
"""

import gc
import logging
from logging.handlers import RotatingFileHandler
import os
import time
from typing import Dict, List, Optional

import requests

try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    print("psycopg2 is required: pip install psycopg2-binary")


# DB settings
DB_HOST = os.environ.get("DB_HOST", "144.24.87.146")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASS = os.environ.get("DB_PASS", "neko15746+")
DB_NAME = os.environ.get("DB_NAME", "rabbit_novel")

# Ollama settings
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "gemma3:12b")
TRANSLATION_ENGINE = os.environ.get("TRANSLATION_ENGINE", f"ollama-{MODEL_NAME}")
MAX_CHUNK_SIZE = int(os.environ.get("MAX_CHUNK_SIZE", "1000"))
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "300"))
OLLAMA_MAX_RETRY = int(os.environ.get("OLLAMA_MAX_RETRY", "3"))
OLLAMA_RETRY_BASE_SEC = float(os.environ.get("OLLAMA_RETRY_BASE_SEC", "1.5"))
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.3"))
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "4096"))

# Runtime tuning
IDLE_SLEEP_SEC = int(os.environ.get("IDLE_SLEEP_SEC", "10"))
ERROR_SLEEP_SEC = int(os.environ.get("ERROR_SLEEP_SEC", "5"))
OPTIMIZE_EVERY = int(os.environ.get("OPTIMIZE_EVERY", "10"))

# Log settings
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ERROR_LOG_NAME = os.environ.get("ERROR_LOG_FILE", "translate_error.log")
ERROR_LOG_FILE = _ERROR_LOG_NAME if os.path.isabs(_ERROR_LOG_NAME) else os.path.join(_BASE_DIR, _ERROR_LOG_NAME)
ERROR_LOG_MAX_MB = int(os.environ.get("ERROR_LOG_MAX_MB", "20"))
ERROR_LOG_BACKUP_COUNT = int(os.environ.get("ERROR_LOG_BACKUP_COUNT", "5"))

LOGGER = logging.getLogger("translate_worker_ollama")
HTTP_SESSION: Optional[requests.Session] = None
LAST_NOVEL_ID: Optional[int] = None


def setup_logger() -> None:
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Console: keep progress visibility
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)

    # File: errors only + size rotation
    file_handler = RotatingFileHandler(
        ERROR_LOG_FILE,
        maxBytes=ERROR_LOG_MAX_MB * 1024 * 1024,
        backupCount=ERROR_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)


def log_info(msg: str) -> None:
    LOGGER.info(msg)


def log_error(msg: str) -> None:
    LOGGER.error(msg)


def get_http_session() -> requests.Session:
    global HTTP_SESSION
    if HTTP_SESSION is None:
        HTTP_SESSION = requests.Session()
    return HTTP_SESSION


def reset_http_session() -> None:
    global HTTP_SESSION
    if HTTP_SESSION is not None:
        HTTP_SESSION.close()
    HTTP_SESSION = requests.Session()


def get_db_connection():
    if not HAS_PSYCOPG2:
        return None
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASS,
            dbname=DB_NAME,
            connect_timeout=10,
        )
        conn.set_session(autocommit=True)
        return conn
    except Exception as e:
        log_error(f"DB connection failed: {e}")
        return None


def ensure_db_connection(conn):
    if conn is None:
        return get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return get_db_connection()


def get_chapter_to_translate(conn, last_novel_id: Optional[int]) -> Optional[Dict]:
    try:
        cur = conn.cursor()
        base_where = """
            c.content IS NOT NULL
            AND c.content != ''
            AND c.status = 'DONE'
            AND NOT EXISTS (
                SELECT 1 FROM chapter_translations ct
                WHERE ct.chapter_id = c.id
                  AND ct.engine = %s
            )
        """
        query = f"""
            SELECT c.id, c.title, c.content, c.novel_id
            FROM chapters c
            JOIN novels n ON c.novel_id = n.id
            WHERE n.source_language = 'ja'
              AND {base_where}
              AND (%s IS NULL OR c.novel_id <> %s)
            ORDER BY c.id ASC
            LIMIT 1
        """
        try:
            cur.execute(query, (TRANSLATION_ENGINE, last_novel_id, last_novel_id))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"""
                    SELECT c.id, c.title, c.content, c.novel_id
                    FROM chapters c
                    JOIN novels n ON c.novel_id = n.id
                    WHERE n.source_language = 'ja'
                      AND {base_where}
                    ORDER BY c.id ASC
                    LIMIT 1
                    """,
                    (TRANSLATION_ENGINE,),
                )
                row = cur.fetchone()
            cur.close()
        except Exception as e:
            if "source_language" in str(e):
                cur.close()
                cur = conn.cursor()
                fallback_query = f"""
                    SELECT c.id, c.title, c.content, c.novel_id
                    FROM chapters c
                    WHERE {base_where}
                      AND (%s IS NULL OR c.novel_id <> %s)
                    ORDER BY c.id ASC
                    LIMIT 1
                """
                cur.execute(fallback_query, (TRANSLATION_ENGINE, last_novel_id, last_novel_id))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        f"""
                        SELECT c.id, c.title, c.content, c.novel_id
                        FROM chapters c
                        WHERE {base_where}
                        ORDER BY c.id ASC
                        LIMIT 1
                        """,
                        (TRANSLATION_ENGINE,),
                    )
                    row = cur.fetchone()
                cur.close()
            else:
                raise

        if row:
            return {"id": row[0], "title": row[1], "content": row[2], "novel_id": row[3]}
        return None
    except Exception as e:
        log_error(f"Failed to load chapter: {e}")
        return None


def save_translation(conn, chapter_id: int, content: str) -> bool:
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chapter_translations (chapter_id, engine, content, created_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (chapter_id, engine)
            DO UPDATE SET content = EXCLUDED.content, created_at = NOW()
            """,
            (chapter_id, TRANSLATION_ENGINE, content),
        )
        cur.close()
        return True
    except Exception as e:
        log_error(f"Failed to save translation: {e}")
        return False


def sanitize_title(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return ""
    parts = [p.strip() for p in t.replace("|", " - ").split(" - ") if p.strip()]
    dedup = []
    for p in parts:
        if not dedup or dedup[-1] != p:
            dedup.append(p)
    t = " - ".join(dedup) if dedup else t
    if len(t) > 180:
        t = t[:180].rstrip()
    return t


def translate_title_ja_to_ko(source_title: str) -> Optional[str]:
    src = (source_title or "").strip()
    if not src:
        return None
    prompt = f"""
Translate this Japanese chapter title to natural Korean.
Output only the translated title.

Japanese title:
{src}

Korean title:
"""
    translated = call_ollama_with_prompt(prompt)
    if not translated:
        return None
    return sanitize_title(translated.splitlines()[0])


def save_translated_title(conn, chapter_id: int, source_title: str) -> None:
    translated = translate_title_ja_to_ko(source_title)
    if not translated:
        return
    try:
        cur = conn.cursor()
        # Keep original and translated title both, but avoid repeated growth.
        merged = f"{sanitize_title(source_title)} | {translated}"
        cur.execute("UPDATE chapters SET title = %s WHERE id = %s", (merged[:220], chapter_id))
        cur.close()
    except Exception as e:
        log_error(f"Failed to update translated title for chapter {chapter_id}: {e}")


def split_text(text: str, max_length: int) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    current_len = 0

    for line in text.splitlines():
        add_len = len(line) + 1
        if current and current_len + add_len > max_length:
            parts.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len

    if current:
        parts.append("\n".join(current))
    return parts


def call_ollama_with_prompt(prompt: str) -> Optional[str]:
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": OLLAMA_TEMPERATURE,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }

    session = get_http_session()
    for attempt in range(1, OLLAMA_MAX_RETRY + 1):
        try:
            response = session.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT_SEC)
            response.raise_for_status()
            translated = response.json().get("response", "").strip()
            if translated:
                return translated
            log_error(f"Ollama empty response (attempt {attempt}/{OLLAMA_MAX_RETRY})")
        except Exception as e:
            log_error(f"Ollama request failed (attempt {attempt}/{OLLAMA_MAX_RETRY}): {e}")

        if attempt < OLLAMA_MAX_RETRY:
            time.sleep(OLLAMA_RETRY_BASE_SEC * attempt)

    return None


def call_ollama(text: str) -> Optional[str]:
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
    return call_ollama_with_prompt(prompt)


def translate_text(full_text: str) -> Optional[str]:
    if not full_text:
        return None

    chunks = split_text(full_text, MAX_CHUNK_SIZE)
    translated_chunks: List[str] = []
    log_info(f"   chunks={len(chunks)}")

    for idx, chunk in enumerate(chunks, start=1):
        if not chunk.strip():
            translated_chunks.append("")
            continue
        log_info(f"   translating chunk {idx}/{len(chunks)} ({len(chunk)} chars)")
        translated = call_ollama(chunk)
        if not translated:
            log_error(f"Chunk translation failed at {idx}/{len(chunks)}")
            return None
        translated_chunks.append(translated)

    return "\n".join(translated_chunks)


def run_periodic_optimization(conn, success_count: int):
    if OPTIMIZE_EVERY <= 0 or success_count % OPTIMIZE_EVERY != 0:
        return conn

    log_info(f"Optimization run triggered after {success_count} translations")
    gc_count = gc.collect()
    reset_http_session()

    try:
        if conn:
            conn.close()
    except Exception:
        pass

    new_conn = get_db_connection()
    if not new_conn:
        log_error("Reconnection failed during optimization; keep retrying in main loop")
        return None

    log_info(f"Optimization done (gc_collected={gc_count})")
    return new_conn


def main() -> None:
    setup_logger()
    log_info("=" * 62)
    log_info(f"Rabbit Translation Worker (Ollama): model={MODEL_NAME}")
    log_info(f"engine={TRANSLATION_ENGINE}, optimize_every={OPTIMIZE_EVERY}")
    log_info("=" * 62)

    if not HAS_PSYCOPG2:
        return

    global LAST_NOVEL_ID
    conn = get_db_connection()
    translated_count = 0

    while True:
        try:
            conn = ensure_db_connection(conn)
            if not conn:
                log_error("DB unavailable; retrying after sleep")
                time.sleep(ERROR_SLEEP_SEC)
                continue

            chapter = get_chapter_to_translate(conn, LAST_NOVEL_ID)
            if not chapter:
                time.sleep(IDLE_SLEEP_SEC)
                continue

            chapter_id = chapter["id"]
            LAST_NOVEL_ID = chapter.get("novel_id")
            title = chapter["title"] or ""
            content = chapter["content"] or ""
            log_info(f"Start chapter id={chapter_id}, len={len(content)}, title={title}")

            started = time.time()
            translated_content = translate_text(content)
            if not translated_content:
                log_error(f"Translation failed for chapter id={chapter_id}")
                time.sleep(ERROR_SLEEP_SEC)
                continue

            if not save_translation(conn, chapter_id, translated_content):
                time.sleep(ERROR_SLEEP_SEC)
                continue

            if title:
                save_translated_title(conn, chapter_id, title)

            translated_count += 1
            elapsed = time.time() - started
            log_info(f"Done chapter id={chapter_id} in {elapsed:.1f}s")

            conn = run_periodic_optimization(conn, translated_count)

        except KeyboardInterrupt:
            log_info("Shutdown requested")
            break
        except Exception as e:
            log_error(f"Unhandled worker error: {e}")
            time.sleep(ERROR_SLEEP_SEC)

    try:
        if conn:
            conn.close()
    except Exception:
        pass

    if HTTP_SESSION is not None:
        HTTP_SESSION.close()


if __name__ == "__main__":
    main()
