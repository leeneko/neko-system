"""
================================================================================
🌐 Rabbit System API Server (server.py)
================================================================================

목적:
  - FastAPI 기반 중계 서버 (OCI 클라우드)
  - 클라이언트(회사 PC)와 워커(집 PC) 간의 통신 중계
  - PostgreSQL 데이터베이스 관리
  - 웹 UI (Jinja2 템플릿) 제공

역할:
  1. 클라이언트 ← API: 소설 목록, 챕터 조회, 읽기
  2. 워커 ↔ API: 작업 큐 관리, 결과 수집
  3. DB: 소설/챕터/장르/사용자 정보 저장

주요 기능:
  - 웹 UI (chapters.html, read.html)
  - REST API (/api/*, /client/*, /worker/*, /web/*)
  - 한글 띄어쓰기 자동 보정 (pykospacing)
  - 테마 지원 (paper, dark, light)
  - 다국어 장르 관리
  - 재크롤 기능

기술:
  - FastAPI: 웹 프레임워크
  - psycopg2: PostgreSQL 드라이버
  - Jinja2: 템플릿 엔진
  - pykospacing: 한글 띄어쓰기

데이터베이스 연결:
  - host: localhost (Docker 내부)
  - port: 5432
  - database: rabbit_novel
  - user: root
  - password: neko15746+

================================================================================
"""

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg2
import os
import json
from html import escape
from urllib.parse import quote, unquote
from typing import Optional, List
from datetime import datetime, timedelta
import logging

# ============================================================================
# 로깅 설정
# ============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# 한글 띄어쓰기 자동 보정 (선택)
# ============================================================================

try:
    from pykospacing import Spacing
    logger.info("✓ pykospacing 설치됨 (한글 띄어쓰기 자동 보정)")
except Exception:
    Spacing = None
    logger.info("✗ pykospacing 미설치 (띄어쓰기 보정 안 함)")

# ============================================================================
# FastAPI 앱 초기화
# ============================================================================

app = FastAPI(title="Rabbit Novel System", version="3.0")

# ============================================================================
# 템플릿 및 정적 파일 설정
# ============================================================================

# Jinja2 템플릿 디렉토리 (HTML 파일들)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# 지원 테마 목록
THEMES = {"paper", "dark", "light"}

# 최근 읽은 책 쿠키 설정
RECENT_COOKIE = "recent_reads"
RECENT_LIMIT = 20
MAX_FAIL_RETRY = int(os.environ.get("MAX_FAIL_RETRY", "5"))
WORKER_STALE_SEC = int(os.environ.get("WORKER_STALE_SEC", "120"))
REQUIRED_WORKER_ROLES = tuple(
    r.strip() for r in os.environ.get("REQUIRED_WORKER_ROLES", "crawler,translator").split(",") if r.strip()
)

# 글로벌 띄어쓰기 모델 (처음 사용 시 로드)
_spacing_model = None

def apply_spacing(text: str) -> str:
    global _spacing_model
    if Spacing is None:
        return text
    if _spacing_model is None:
        _spacing_model = Spacing()
    try:
        # 줄바꿈은 유지하고, 각 줄 단위로 띄어쓰기만 보정
        parts = []
        for chunk in text.splitlines(keepends=True):
            if chunk.endswith("\n"):
                line = chunk[:-1]
                if line.strip():
                    parts.append(_spacing_model(line) + "\n")
                else:
                    parts.append(chunk)
            else:
                if chunk.strip():
                    parts.append(_spacing_model(chunk))
                else:
                    parts.append(chunk)
        return "".join(parts)
    except Exception:
        return text

def get_theme(request: Request) -> str:
    theme = request.cookies.get("theme", "paper")
    return theme if theme in THEMES else "paper"

def normalize_genre(name: str) -> str:
    return name.strip()

def parse_genres(items: List[str], custom: str, limit: int = 3) -> List[str]:
    genres = []
    for g in items:
        g = normalize_genre(g)
        if g and g not in genres:
            genres.append(g)
    if custom:
        for g in custom.split(","):
            g = normalize_genre(g)
            if g and g not in genres:
                genres.append(g)
    return genres[:limit]

def fetch_all_genres(cur) -> List[str]:
    cur.execute("SELECT name FROM genres ORDER BY name ASC")
    return [r[0] for r in cur.fetchall()]

def fetch_all_genres_with_ids(cur):
    cur.execute("SELECT id, name FROM genres ORDER BY name ASC")
    return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

def fetch_novel_genres(cur, novel_ids: List[int]):
    if not novel_ids:
        return {}
    cur.execute("""
        SELECT ng.novel_id, g.name
        FROM novel_genres ng
        JOIN genres g ON g.id = ng.genre_id
        WHERE ng.novel_id = ANY(%s)
        ORDER BY g.name ASC
    """, (novel_ids,))
    rows = cur.fetchall()
    mapping = {}
    for novel_id, name in rows:
        mapping.setdefault(novel_id, []).append(name)
    return mapping

def fetch_novel_genres_with_ids(cur, novel_id: int):
    cur.execute("""
        SELECT g.id, g.name
        FROM novel_genres ng
        JOIN genres g ON g.id = ng.genre_id
        WHERE ng.novel_id = %s
        ORDER BY g.name ASC
    """, (novel_id,))
    return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

def upsert_novel_genres(cur, novel_id: int, genres: List[str]):
    cur.execute("DELETE FROM novel_genres WHERE novel_id = %s", (novel_id,))
    for name in genres:
        cur.execute("INSERT INTO genres (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        cur.execute("SELECT id FROM genres WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "INSERT INTO novel_genres (novel_id, genre_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (novel_id, row[0])
            )

def get_recent_reads(request: Request):
    raw = request.cookies.get(RECENT_COOKIE)
    if not raw:
        return []
    try:
        data = json.loads(unquote(raw))
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []

def set_recent_reads(response: HTMLResponse, items):
    trimmed = items[:RECENT_LIMIT]
    payload = quote(json.dumps(trimmed, ensure_ascii=False))
    response.set_cookie(RECENT_COOKIE, payload, max_age=60 * 60 * 24 * 365, path="/")

def sanitize_chapter_title(raw_title: Optional[str], novel_title: Optional[str] = None) -> str:
    title = (raw_title or "").strip()
    if not title:
        return "제목 없음"

    # Remove common site suffixes/prefixes and normalize separators.
    title = title.replace("::", ":")
    noise_tokens = ["북토끼", "booktoki", "syosetu", "소설가가되자", "narou"]
    parts = [p.strip() for p in title.replace("|", " - ").split(" - ") if p.strip()]
    filtered = []
    for part in parts:
        low = part.lower()
        if any(tok in low for tok in noise_tokens):
            continue
        filtered.append(part)
    if filtered:
        title = " - ".join(filtered)

    # If novel title is repeated inside chapter title, collapse it.
    if novel_title:
        n = novel_title.strip()
        if n and n in title:
            title = title.replace(n, "").strip(" -:[]()")
            if not title:
                title = n

    # Collapse repeated adjacent phrases: "A - A - B" -> "A - B"
    dedup = []
    for part in [p.strip() for p in title.split(" - ") if p.strip()]:
        if not dedup or dedup[-1] != part:
            dedup.append(part)
    title = " - ".join(dedup) if dedup else (raw_title or "").strip()

    if len(title) > 180:
        title = title[:180].rstrip()
    return title or "제목 없음"

# --- 데이터 모델 ---
class NovelReq(BaseModel):
    title: str
    url: str

class JobResult(BaseModel):
    chapter_id: int
    url: str
    title: str
    content: str
    next_url: Optional[str] = None
    translation: Optional[str] = None
    translation_engine: Optional[str] = None
    translated_title: Optional[str] = None

class JobFail(BaseModel):
    chapter_id: int
    reason: Optional[str] = None
    url: Optional[str] = None

class WorkerState(BaseModel):
    worker_name: str
    role: str
    status: str
    novel_id: Optional[int] = None
    chapter_id: Optional[int] = None
    chapter_title: Optional[str] = None
    note: Optional[str] = None
    updated_at: Optional[str] = None

# --- DB 연결 ---
def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "db"),
        port="5432",
        user=os.environ.get("DB_USER", "kcc_user"),
        password=os.environ.get("DB_PASS", "kcc_password"),
        dbname=os.environ.get("DB_NAME", "rabbit_novel")
    )

def _utc_now() -> datetime:
    return datetime.utcnow()

def _parse_iso_dt(raw: Optional[str]) -> datetime:
    if not raw:
        return _utc_now()
    try:
        return datetime.fromisoformat(raw.replace("Z", ""))
    except Exception:
        return _utc_now()

def upsert_worker_state(cur, data: WorkerState):
    # Use server-side receive time to avoid client clock drift causing false stale/red status.
    dt = _utc_now()
    cur.execute(
        """
        INSERT INTO worker_states
            (worker_name, role, status, novel_id, chapter_id, chapter_title, note, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (worker_name)
        DO UPDATE SET
            role = EXCLUDED.role,
            status = EXCLUDED.status,
            novel_id = EXCLUDED.novel_id,
            chapter_id = EXCLUDED.chapter_id,
            chapter_title = EXCLUDED.chapter_title,
            note = EXCLUDED.note,
            updated_at = EXCLUDED.updated_at
        """,
        (
            data.worker_name,
            data.role,
            data.status,
            data.novel_id,
            data.chapter_id,
            data.chapter_title,
            data.note,
            dt,
        ),
    )

def fetch_worker_states(cur):
    cur.execute(
        """
        SELECT worker_name, role, status, novel_id, chapter_id, chapter_title, note, updated_at
        FROM worker_states
        ORDER BY role ASC, worker_name ASC
        """
    )
    rows = cur.fetchall()
    items = []
    now = _utc_now()
    for r in rows:
        updated_at = r[7]
        stale = (now - updated_at) > timedelta(seconds=WORKER_STALE_SEC) if updated_at else True
        items.append({
            "worker_name": r[0],
            "role": r[1],
            "status": r[2],
            "novel_id": r[3],
            "chapter_id": r[4],
            "chapter_title": r[5],
            "note": r[6],
            "updated_at": updated_at.isoformat() if updated_at else None,
            "stale": stale,
        })
    return items

def is_worker_system_ok(states) -> bool:
    if not states:
        return False
    roles = {s["role"] for s in states}
    if REQUIRED_WORKER_ROLES and not all(role in roles for role in REQUIRED_WORKER_ROLES):
        return False
    for s in states:
        if s["stale"]:
            return False
        if s["status"] in ("ERROR", "DOWN"):
            return False
    return True

def get_worker_health_snapshot():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        states = fetch_worker_states(cur)
        ok = is_worker_system_ok(states)
        return ok, states
    finally:
        conn.close()

def fetch_runtime_errors(cur, limit: int = 100):
    cur.execute(
        """
        SELECT
            c.id, c.novel_id, n.title, c.title, c.status, COALESCE(c.fail_count, 0), COALESCE(c.last_error, ''),
            CASE WHEN c.content IS NOT NULL AND c.content <> '' THEN TRUE ELSE FALSE END AS has_content
        FROM chapters c
        JOIN novels n ON n.id = c.novel_id
        WHERE c.status = 'FAILED' OR COALESCE(c.last_error, '') <> ''
        ORDER BY c.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    return [
        {
            "chapter_id": r[0],
            "novel_id": r[1],
            "novel_title": r[2],
            "chapter_title": r[3],
            "status": r[4],
            "fail_count": r[5],
            "last_error": r[6],
            "has_content": bool(r[7]),
        }
        for r in rows
    ]

def fetch_runtime_request_progress(cur, limit: int = 200):
    cur.execute(
        """
        SELECT
            n.id,
            n.title,
            COUNT(c.id) AS total_count,
            COUNT(*) FILTER (WHERE c.status = 'DONE') AS done_count,
            COUNT(*) FILTER (WHERE c.status = 'PENDING') AS pending_count,
            COUNT(*) FILTER (WHERE c.status = 'FAILED') AS failed_count,
            MAX(c.id) AS latest_chapter_id
        FROM novels n
        LEFT JOIN chapters c ON c.novel_id = n.id
        GROUP BY n.id, n.title
        ORDER BY n.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    items = []
    for r in rows:
        total = int(r[2] or 0)
        done = int(r[3] or 0)
        pending = int(r[4] or 0)
        failed = int(r[5] or 0)
        crawled = done + failed
        crawl_percent = round((crawled / total) * 100, 1) if total > 0 else 0.0
        items.append(
            {
                "novel_id": r[0],
                "novel_title": r[1],
                "total_count": total,
                "done_count": done,
                "pending_count": pending,
                "failed_count": failed,
                "crawled_count": crawled,
                "crawl_percent": crawl_percent,
                "latest_chapter_id": r[6],
            }
        )
    return items

def fetch_runtime_translation_progress(cur, limit: int = 200):
    cur.execute(
        """
        SELECT
            n.id,
            n.title,
            COUNT(c.id) FILTER (
                WHERE c.status = 'DONE'
                  AND COALESCE(c.content, '') <> ''
            ) AS ready_count,
            COUNT(DISTINCT c.id) FILTER (
                WHERE c.status = 'DONE'
                  AND COALESCE(c.content, '') <> ''
                  AND ct.chapter_id IS NOT NULL
            ) AS translated_count,
            MAX(c.id) FILTER (
                WHERE c.status = 'DONE'
                  AND COALESCE(c.content, '') <> ''
            ) AS latest_ready_chapter_id
        FROM novels n
        LEFT JOIN chapters c ON c.novel_id = n.id
        LEFT JOIN chapter_translations ct ON ct.chapter_id = c.id
        GROUP BY n.id, n.title
        HAVING COUNT(c.id) FILTER (
            WHERE c.status = 'DONE'
              AND COALESCE(c.content, '') <> ''
        ) > 0
        ORDER BY n.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    items = []
    for r in rows:
        ready = int(r[2] or 0)
        translated = int(r[3] or 0)
        remaining = max(ready - translated, 0)
        percent = round((translated / ready) * 100, 1) if ready > 0 else 0.0
        items.append(
            {
                "novel_id": r[0],
                "novel_title": r[1],
                "ready_count": ready,
                "translated_count": translated,
                "remaining_count": remaining,
                "translation_percent": percent,
                "in_progress": remaining > 0,
                "latest_ready_chapter_id": r[4],
            }
        )
    return items

# ... [기존 startup_event, health_check 코드는 동일] ...
@app.on_event("startup")
def startup_event():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS novels (
            id SERIAL PRIMARY KEY,
            title TEXT,
            list_url TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chapters (
            id SERIAL PRIMARY KEY,
            novel_id INTEGER,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            content TEXT,
            status TEXT DEFAULT 'PENDING',
            next_url TEXT,
            fail_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Online-safe migrations for existing DBs
    cur.execute("ALTER TABLE chapters ADD COLUMN IF NOT EXISTS fail_count INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE chapters ADD COLUMN IF NOT EXISTS last_error TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chapter_translations (
            id SERIAL PRIMARY KEY,
            chapter_id INTEGER NOT NULL,
            engine TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (chapter_id, engine)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS genres (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS novel_genres (
            novel_id INTEGER NOT NULL,
            genre_id INTEGER NOT NULL,
            UNIQUE (novel_id, genre_id)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS worker_states (
            worker_name TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            novel_id INTEGER,
            chapter_id INTEGER,
            chapter_title TEXT,
            note TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

# =========================================================
# [기능 1] 소설 업데이트 (재확인) 로직
# =========================================================
@app.post("/client/update_all")
def update_all_novels():
    """모든 소설의 마지막 화를 PENDING으로 변경하여 갱신 유도"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 각 소설별로 가장 최근 챕터 ID 조회
        cur.execute("""
            SELECT id FROM chapters c1
            WHERE id = (
                SELECT MAX(id) FROM chapters c2 WHERE c2.novel_id = c1.novel_id
            )
        """)
        rows = cur.fetchall()
        
        count = 0
        for row in rows:
            # 상태를 PENDING으로 변경 -> 워커가 다시 방문 -> 다음화 있으면 자동 등록됨
            cur.execute("UPDATE chapters SET status = 'PENDING', fail_count = 0, last_error = NULL WHERE id = %s", (row[0],))
            count += 1
            
        conn.commit()
        return {"status": "success", "msg": f"{count}개의 소설 업데이트 요청됨"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}
    finally:
        conn.close()

# =========================================================
# [기능 2] 모바일 웹 뷰어 (HTML)
# =========================================================

# 1. 소설 목록 (메인)
@app.get("/web", response_class=HTMLResponse)
def web_index(request: Request):
    conn = get_db_connection()
    cur = conn.cursor()
    selected_genre = request.query_params.get("genre")
    if selected_genre:
        cur.execute("""
            SELECT n.id, n.title
            FROM novels n
            JOIN novel_genres ng ON ng.novel_id = n.id
            JOIN genres g ON g.id = ng.genre_id
            WHERE g.name = %s
            ORDER BY n.id DESC
        """, (selected_genre,))
    else:
        cur.execute("SELECT id, title FROM novels ORDER BY id DESC")
    novels = [{"id": r[0], "title": r[1]} for r in cur.fetchall()]
    novel_ids = [n["id"] for n in novels]
    novel_genres = fetch_novel_genres(cur, novel_ids)
    all_genres = fetch_all_genres(cur)
    conn.close()
    last_novel_id = request.cookies.get("last_novel_id")
    if last_novel_id:
        try:
            last_id = int(last_novel_id)
            novels = sorted(
                novels,
                key=lambda n: 0 if n["id"] == last_id else 1
            )
        except ValueError:
            pass
    genre_slug_map = {g: quote(g) for g in all_genres}
    current_url = request.url.path
    if request.url.query:
        current_url = f"{current_url}?{request.url.query}"
    current_url_q = quote(current_url, safe="")
    worker_ok, _ = get_worker_health_snapshot()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "novels": novels,
            "theme": get_theme(request),
            "worker_ok": worker_ok,
            "last_novel_id": last_novel_id,
            "recent_reads": get_recent_reads(request),
            "novel_genres": novel_genres,
            "all_genres": all_genres,
            "selected_genre": selected_genre,
            "genre_slug_map": genre_slug_map,
            "current_url_q": current_url_q
        }
    )

# 2. 회차 목록
@app.get("/web/novel/{novel_id}", response_class=HTMLResponse)
def web_chapters(request: Request, novel_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    # 소설 제목
    cur.execute("SELECT title FROM novels WHERE id = %s", (novel_id,))
    novel_title = cur.fetchone()[0]
    
    # 챕터 목록
    cur.execute("SELECT id, title, status FROM chapters WHERE novel_id = %s ORDER BY id ASC", (novel_id,))
    chapters = [{"id": r[0], "title": r[1], "status": r[2]} for r in cur.fetchall()]
    novel_genres = fetch_novel_genres_with_ids(cur, novel_id)
    all_genres = fetch_all_genres_with_ids(cur)
    conn.close()
    worker_ok, _ = get_worker_health_snapshot()
    response = templates.TemplateResponse(
        "chapters.html",
        {
            "request": request,
            "novel_title": novel_title,
            "chapters": chapters,
            "theme": get_theme(request),
            "worker_ok": worker_ok,
            "novel_id": novel_id,
            "novel_genres": novel_genres,
            "all_genres": all_genres
        }
    )
    response.set_cookie("last_novel_id", str(novel_id), max_age=60 * 60 * 24 * 365, path="/")
    return response

# 3. 뷰어
@app.get("/web/read/{chapter_id}", response_class=HTMLResponse)
def web_read(request: Request, chapter_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, content, next_url, novel_id, url FROM chapters WHERE id = %s", (chapter_id,))
    row = cur.fetchone()
    
    if not row: 
        conn.close()
        return HTMLResponse("존재하지 않는 챕터입니다.")
    
    title, content, next_url, novel_id, chapter_url = row
    cur.execute("SELECT title FROM novels WHERE id = %s", (novel_id,))
    novel_title_row = cur.fetchone()
    novel_title = novel_title_row[0] if novel_title_row else "알 수 없음"
    
    # 다음 화 ID 찾기
    next_chapter_id = None
    if next_url:
        cur.execute("SELECT id FROM chapters WHERE url = %s", (next_url,))
        res = cur.fetchone()
        if res: next_chapter_id = res[0]
        
    view_mode = request.query_params.get("view", "original")
    if view_mode not in ("original", "mixed", "translated"):
        view_mode = "original"

    translated_content = None
    cur.execute("""
        SELECT content FROM chapter_translations
        WHERE chapter_id = %s
        ORDER BY created_at DESC
        LIMIT 1
    """, (chapter_id,))
    trow = cur.fetchone()
    if trow:
        translated_content = trow[0]

    conn.close()
    
    # 본문 줄바꿈 처리
    if request.query_params.get("spacing") == "1":
        content = apply_spacing(content)
    
    view_mode = request.query_params.get("view")
    if not view_mode:
        # 쿠키나 파라미터가 없으면, 번역본이 있으면 'combined', 없으면 'original'
        view_mode = "combined" if translated_content else "original"
    
    def split_paragraphs(text: Optional[str]) -> List[str]:
        if not text:
            return []
        paras = []
        chunk = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                if chunk:
                    paras.append(" ".join(chunk).strip())
                    chunk = []
                continue
            chunk.append(line)
        if chunk:
            paras.append(" ".join(chunk).strip())
        return [p for p in paras if p]

    def build_mixed_html(original_text: str, translated_text: str) -> str:
        orig_lines = split_paragraphs(original_text)
        trans_lines = split_paragraphs(translated_text)
        max_len = max(len(orig_lines), len(trans_lines), 1)
        parts = []
        for i in range(max_len):
            o = orig_lines[i] if i < len(orig_lines) else ""
            t = trans_lines[i] if i < len(trans_lines) else ""
            if not o.strip():
                o = "\u00a0"
            if not t.strip():
                t = "\u00a0"
            parts.append(
                f'<div class="paragraph-pair"><div class="line original">{escape(o)}</div>'
                f'<div class="line translated">{escape(t)}</div></div>'
            )
        return "\n".join(parts)

    def build_paragraph_html(text: str) -> str:
        parts = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts.append(f"<p>{escape(line)}</p>")
        return "".join(parts) if parts else "<p></p>"

    content_html = build_paragraph_html(content)
    translated_html = build_paragraph_html(translated_content) if translated_content else None
    mixed_html = build_mixed_html(content, translated_content) if translated_content else None
    
    worker_ok, _ = get_worker_health_snapshot()
    response = templates.TemplateResponse("read.html", {
        "request": request,
        "title": title,
        "content": content_html,
        "translated_content": translated_html,
        "mixed_content": mixed_html,
        "view_mode": view_mode,
        "next_chapter_id": next_chapter_id,
        "novel_id": novel_id,
        "chapter_id": chapter_id,
        "chapter_url": chapter_url,
        "theme": get_theme(request),
        "worker_ok": worker_ok
    })

    response.set_cookie("last_novel_id", str(novel_id), max_age=60 * 60 * 24 * 365, path="/")
    recent = get_recent_reads(request)
    recent = [r for r in recent if r.get("novel_id") != novel_id]
    recent.insert(0, {
        "novel_id": novel_id,
        "novel_title": novel_title,
        "chapter_id": chapter_id,
        "chapter_title": title
    })
    set_recent_reads(response, recent)
    return response

@app.get("/web/read/{chapter_id}/retranslate")
def retranslate_chapter(request: Request, chapter_id: int):
    """기존 번역 삭제 후 재번역 요청"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1. 기존 번역 삭제
        cur.execute("DELETE FROM chapter_translations WHERE chapter_id = %s", (chapter_id,))
        # 재번역은 크롤 재요청이 아니라 번역 워커가 가져가도록 기존 본문 상태 유지
        cur.execute("UPDATE chapters SET status = 'DONE', fail_count = 0, last_error = NULL WHERE id = %s", (chapter_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        # 에러 처리 (로그 등)
    finally:
        conn.close()
    
    # 뷰어 페이지로 리다이렉트 (번역 중 메시지가 뜨도록)
    return RedirectResponse(url=f"/web/read/{chapter_id}")

@app.get("/web/novel/{novel_id}/refresh")
def web_refresh_novel(request: Request, novel_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id FROM chapters c1
            WHERE id = (
                SELECT MAX(id) FROM chapters c2 WHERE c2.novel_id = c1.novel_id
            ) AND novel_id = %s
        """, (novel_id,))
        last_row = cur.fetchone()
        if last_row:
            cur.execute("UPDATE chapters SET status = 'PENDING', fail_count = 0, last_error = NULL WHERE id = %s", (last_row[0],))
            conn.commit()
    finally:
        conn.close()
    next_url = request.query_params.get("next", "/web")
    if not next_url.startswith("/"):
        next_url = "/web"
    return RedirectResponse(url=next_url)

@app.get("/web/read/{chapter_id}/recrawl")
def recrawl_chapter(request: Request, chapter_id: int):
    """Mark a chapter for recrawl and redirect back to read"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT novel_id FROM chapters WHERE id = %s", (chapter_id,))
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM chapter_translations WHERE chapter_id = %s", (chapter_id,))
            cur.execute("UPDATE chapters SET status = 'PENDING', fail_count = 0, last_error = NULL WHERE id = %s", (chapter_id,))
            conn.commit()
    finally:
        conn.close()
    
    # Redirect back to read page
    return RedirectResponse(url=f"/web/read/{chapter_id}")

@app.get("/web/refresh_all")
def web_refresh_all(request: Request):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id FROM chapters c1
            WHERE id = (
                SELECT MAX(id) FROM chapters c2 WHERE c2.novel_id = c1.novel_id
            )
        """)
        rows = cur.fetchall()
        for row in rows:
            cur.execute("UPDATE chapters SET status = 'PENDING', fail_count = 0, last_error = NULL WHERE id = %s", (row[0],))
        conn.commit()
    finally:
        conn.close()
    next_url = request.query_params.get("next", "/web")
    if not next_url.startswith("/"):
        next_url = "/web"
    return RedirectResponse(url=next_url)

@app.get("/web/theme/{theme}")
def set_theme(request: Request, theme: str):
    theme = theme if theme in THEMES else "paper"
    next_url = request.query_params.get("next", "/web")
    if not next_url.startswith("/"):
        next_url = "/web"
    response = RedirectResponse(url=next_url)
    response.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365, path="/")
    return response

@app.get("/web/history/remove/{chapter_id}")
def remove_history(request: Request, chapter_id: int):
    recent = get_recent_reads(request)
    recent = [r for r in recent if r.get("chapter_id") != chapter_id]
    next_url = request.query_params.get("next", "/web")
    if not next_url.startswith("/"):
        next_url = "/web"
    response = RedirectResponse(url=next_url)
    set_recent_reads(response, recent)
    return response

@app.post("/web/novel/{novel_id}/genres")
def add_genres_to_novel(
    novel_id: int,
    genres: List[str] = Form([]),
    custom_genres: str = Form("")
):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        existing = fetch_novel_genres(cur, [novel_id]).get(novel_id, [])
        merged = parse_genres(existing + genres, custom_genres)
        upsert_novel_genres(cur, novel_id, merged)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/web/novel/{novel_id}", status_code=303)

@app.get("/web/novel/{novel_id}/genres/remove/{genre_id}")
def remove_genre_from_novel(novel_id: int, genre_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM novel_genres WHERE novel_id = %s AND genre_id = %s", (novel_id, genre_id))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/web/novel/{novel_id}", status_code=303)

@app.get("/web/request", response_class=HTMLResponse)
def web_request(request: Request):
    conn = get_db_connection()
    cur = conn.cursor()
    all_genres = fetch_all_genres(cur)
    conn.close()
    worker_ok, _ = get_worker_health_snapshot()
    return templates.TemplateResponse(
        "request.html",
        {
            "request": request,
            "theme": get_theme(request),
            "worker_ok": worker_ok,
            "status": None,
            "all_genres": all_genres
        }
    )

@app.post("/web/request", response_class=HTMLResponse)
def web_request_submit(
    request: Request,
    title: str = Form(...),
    url: str = Form(...),
    genres: List[str] = Form([]),
    custom_genres: str = Form("")
):
    conn = get_db_connection()
    cur = conn.cursor()
    status = {"type": "success", "msg": "요청이 등록되었습니다."}
    all_genres = []
    try:
        cur.execute(
            "INSERT INTO novels (title, list_url) VALUES (%s, %s) ON CONFLICT (list_url) DO NOTHING",
            (title, url)
        )
        cur.execute("SELECT id FROM novels WHERE list_url = %s", (url,))
        row = cur.fetchone()
        if not row:
            raise Exception("소설 ID를 찾을 수 없습니다.")
        novel_id = row[0]
        cur.execute(
            "INSERT INTO chapters (novel_id, url, status) VALUES (%s, %s, 'PENDING') ON CONFLICT (url) DO NOTHING",
            (novel_id, url)
        )
        genre_list = parse_genres(genres, custom_genres)
        if genre_list:
            upsert_novel_genres(cur, novel_id, genre_list)
        # 이미 존재하는 소설이라면 마지막 챕터를 재확인하도록 PENDING 처리
        cur.execute("""
            SELECT id FROM chapters c1
            WHERE id = (
                SELECT MAX(id) FROM chapters c2 WHERE c2.novel_id = c1.novel_id
            ) AND novel_id = %s
        """, (novel_id,))
        last_row = cur.fetchone()
        if last_row:
            cur.execute("UPDATE chapters SET status = 'PENDING', fail_count = 0, last_error = NULL WHERE id = %s", (last_row[0],))
        conn.commit()
        all_genres = fetch_all_genres(cur)
    except Exception as e:
        conn.rollback()
        status = {"type": "error", "msg": f"오류: {str(e)}"}
    finally:
        conn.close()
    if not all_genres:
        conn2 = get_db_connection()
        cur2 = conn2.cursor()
        all_genres = fetch_all_genres(cur2)
        conn2.close()
    worker_ok, _ = get_worker_health_snapshot()
    return templates.TemplateResponse(
        "request.html",
        {"request": request, "theme": get_theme(request), "worker_ok": worker_ok, "status": status, "all_genres": all_genres}
    )

@app.post("/client/add")
def add_novel(data: NovelReq):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO novels (title, list_url) VALUES (%s, %s) ON CONFLICT (list_url) DO NOTHING", (data.title, data.url))
        cur.execute("SELECT id FROM novels WHERE list_url = %s", (data.url,))
        novel_id = cur.fetchone()[0]
        cur.execute("INSERT INTO chapters (novel_id, url, status) VALUES (%s, %s, 'PENDING') ON CONFLICT (url) DO NOTHING", (novel_id, data.url))
        cur.execute("""
            SELECT id FROM chapters c1
            WHERE id = (
                SELECT MAX(id) FROM chapters c2 WHERE c2.novel_id = c1.novel_id
            ) AND novel_id = %s
        """, (novel_id,))
        last_row = cur.fetchone()
        if last_row:
            cur.execute("UPDATE chapters SET status = 'PENDING', fail_count = 0, last_error = NULL WHERE id = %s", (last_row[0],))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "msg": str(e)}
    finally: conn.close()

@app.get("/client/list")
def list_novels():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, title FROM novels ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1]} for r in rows]

@app.get("/client/chapters/{novel_id}")
def list_chapters_api(novel_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, title, status FROM chapters WHERE novel_id = %s ORDER BY id ASC", (novel_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1] if r[1] else '로딩중...', "status": r[2]} for r in rows]

@app.get("/client/read/{chapter_id}")
def read_chapter_api(chapter_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, content, status FROM chapters WHERE id = %s", (chapter_id,))
    row = cur.fetchone()
    conn.close()
    if not row: return {"status": "error", "msg": "없음"}
    if row[2] == 'PENDING': return {"status": "pending", "msg": "다운로드 중..."}
    return {"status": "success", "title": row[0], "content": row[1]}

@app.get("/worker/get")
def get_job():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, url, novel_id FROM chapters WHERE status = 'PENDING' ORDER BY id ASC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if row: return {"exists": True, "chapter_id": row[0], "url": row[1], "novel_id": row[2]}
    else: return {"exists": False}

@app.post("/worker/submit")
def submit_job(data: JobResult):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT n.title FROM chapters c JOIN novels n ON c.novel_id = n.id WHERE c.id = %s", (data.chapter_id,))
    novel_row = cur.fetchone()
    novel_title = novel_row[0] if novel_row else None

    raw_title = data.translated_title if data.translated_title else data.title
    final_title = sanitize_chapter_title(raw_title, novel_title=novel_title)

    cur.execute(
        "UPDATE chapters SET title = %s, content = %s, next_url = %s, status = 'DONE', fail_count = 0, last_error = NULL WHERE id = %s",
        (final_title, data.content, data.next_url, data.chapter_id),
    )

    cur.execute("SELECT novel_id FROM chapters WHERE id = %s", (data.chapter_id,))

    row = cur.fetchone()
    if row and data.next_url and "http" in data.next_url:
        novel_id = row[0]
        cur.execute("INSERT INTO chapters (novel_id, url, status) VALUES (%s, %s, 'PENDING') ON CONFLICT (url) DO NOTHING", (novel_id, data.next_url))
    
    if data.translation:
        engine = data.translation_engine or "unknown"
        cur.execute("""
            INSERT INTO chapter_translations (chapter_id, engine, content)
            VALUES (%s, %s, %s)
            ON CONFLICT (chapter_id, engine) DO UPDATE SET content = EXCLUDED.content, created_at = CURRENT_TIMESTAMP
        """, (data.chapter_id, engine, data.translation))
    
    conn.commit()
    conn.close()

    return {"status": "success"}

@app.post("/worker/fail")
def fail_job(data: JobFail):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(fail_count, 0) FROM chapters WHERE id = %s", (data.chapter_id,))
        row = cur.fetchone()
        fail_count = (row[0] if row else 0) + 1
        next_status = "FAILED" if fail_count >= MAX_FAIL_RETRY else "PENDING"
        cur.execute(
            "UPDATE chapters SET status = %s, fail_count = %s, last_error = %s WHERE id = %s",
            (next_status, fail_count, (data.reason or "")[:400], data.chapter_id),
        )
        conn.commit()
        return {"status": "success", "fail_count": fail_count, "next_status": next_status}
    except Exception as e:
        conn.rollback()
        return {"status":"error","msg": str(e)}
    finally:
        conn.close()

@app.post("/worker/state")
def update_worker_state(data: WorkerState):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        upsert_worker_state(cur, data)
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "msg": str(e)}
    finally:
        conn.close()

@app.get("/web/runtime", response_class=HTMLResponse)
def web_runtime(request: Request):
    worker_ok, states = get_worker_health_snapshot()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        errors = fetch_runtime_errors(cur)
        request_progress = fetch_runtime_request_progress(cur)
        translation_progress = fetch_runtime_translation_progress(cur)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "runtime.html",
        {
            "request": request,
            "theme": get_theme(request),
            "worker_ok": worker_ok,
            "states": states,
            "errors": errors,
            "request_progress": request_progress,
            "translation_progress": translation_progress,
        },
    )

@app.get("/web/runtime/data")
def web_runtime_data():
    worker_ok, states = get_worker_health_snapshot()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        errors = fetch_runtime_errors(cur)
        request_progress = fetch_runtime_request_progress(cur)
        translation_progress = fetch_runtime_translation_progress(cur)
    finally:
        conn.close()
    return {
        "worker_ok": worker_ok,
        "states": states,
        "errors": errors,
        "request_progress": request_progress,
        "translation_progress": translation_progress,
        "updated_at": _utc_now().isoformat(),
    }

@app.get("/web/runtime/retry/{chapter_id}")
def web_runtime_retry(chapter_id: int, mode: str = "recrawl"):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(content, '') FROM chapters WHERE id = %s", (chapter_id,))
        row = cur.fetchone()
        has_content = bool(row and row[0])

        if mode == "retranslate" and has_content:
            cur.execute("DELETE FROM chapter_translations WHERE chapter_id = %s", (chapter_id,))
            cur.execute("UPDATE chapters SET status = 'DONE', fail_count = 0, last_error = NULL WHERE id = %s", (chapter_id,))
        else:
            cur.execute("DELETE FROM chapter_translations WHERE chapter_id = %s", (chapter_id,))
            cur.execute("UPDATE chapters SET status = 'PENDING', fail_count = 0, last_error = NULL WHERE id = %s", (chapter_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/web/runtime")

@app.get("/common.css", include_in_schema=False)
def common_css():
    return FileResponse(os.path.join(TEMPLATES_DIR, "common.css"), media_type="text/css")

@app.get("/scripts.js", include_in_schema=False)
def scripts_js():
    return FileResponse(os.path.join(TEMPLATES_DIR, "scripts.js"), media_type="application/javascript")
