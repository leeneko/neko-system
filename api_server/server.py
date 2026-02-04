from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import psycopg2
import os
import json
from urllib.parse import quote, unquote
from typing import Optional, List

try:
    from pykospacing import Spacing
except Exception:
    Spacing = None

app = FastAPI()

# 템플릿 설정 (모바일 웹용)
templates = Jinja2Templates(directory="templates")
THEMES = {"paper", "dark", "light"}
RECENT_COOKIE = "recent_reads"
RECENT_LIMIT = 20
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

# --- DB 연결 ---
def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "db"),
        port="5432",
        user=os.environ.get("DB_USER", "kcc_user"),
        password=os.environ.get("DB_PASS", "kcc_password"),
        dbname=os.environ.get("DB_NAME", "rabbit_novel")
    )

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
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
            cur.execute("UPDATE chapters SET status = 'PENDING' WHERE id = %s", (row[0],))
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
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "novels": novels,
            "theme": get_theme(request),
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
    response = templates.TemplateResponse(
        "chapters.html",
        {
            "request": request,
            "novel_title": novel_title,
            "chapters": chapters,
            "theme": get_theme(request),
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
    cur.execute("SELECT title, content, next_url, novel_id FROM chapters WHERE id = %s", (chapter_id,))
    row = cur.fetchone()
    
    if not row: return HTMLResponse("존재하지 않는 챕터입니다.")
    
    title, content, next_url, novel_id = row
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
    if view_mode in ("mixed", "translated"):
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
    content_html = content.replace("\n", "<br>")
    translated_html = translated_content.replace("\n", "<br>") if translated_content else None
    
    response = templates.TemplateResponse("read.html", {
        "request": request,
        "title": title,
        "content": content_html,
        "translated_content": translated_html,
        "view_mode": view_mode,
        "next_chapter_id": next_chapter_id,
        "novel_id": novel_id,
        "chapter_id": chapter_id,
        "theme": get_theme(request)
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
            cur.execute("UPDATE chapters SET status = 'PENDING' WHERE id = %s", (last_row[0],))
            conn.commit()
    finally:
        conn.close()
    next_url = request.query_params.get("next", "/web")
    if not next_url.startswith("/"):
        next_url = "/web"
    return RedirectResponse(url=next_url)

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
            cur.execute("UPDATE chapters SET status = 'PENDING' WHERE id = %s", (row[0],))
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
    return templates.TemplateResponse(
        "request.html",
        {
            "request": request,
            "theme": get_theme(request),
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
            cur.execute("UPDATE chapters SET status = 'PENDING' WHERE id = %s", (last_row[0],))
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
    return templates.TemplateResponse(
        "request.html",
        {"request": request, "theme": get_theme(request), "status": status, "all_genres": all_genres}
    )

# =========================================================
# [기존 API] Client / Worker (그대로 유지)
# =========================================================
# ... (기존 add_novel, list_novels, submit_job 등 코드는 그대로 두세요) ...
# (코드가 길어서 생략했습니다. 위에서 작성한 기존 로직과 동일하게 유지하면 됩니다.)
# (다만, imports 부분은 위쪽 내용을 따르세요)

# ----------------- (이 아래는 기존 코드 복붙용) -----------------
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
            cur.execute("UPDATE chapters SET status = 'PENDING' WHERE id = %s", (last_row[0],))
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
    cur.execute("UPDATE chapters SET title = %s, content = %s, next_url = %s, status = 'DONE' WHERE id = %s", (data.title, data.content, data.next_url, data.chapter_id))
    cur.execute("SELECT novel_id FROM chapters WHERE id = %s", (data.chapter_id,))
    row = cur.fetchone()
    if row and data.next_url and "http" in data.next_url:
        novel_id = row[0]
        cur.execute("INSERT INTO chapters (novel_id, url, status) VALUES (%s, %s, 'PENDING') ON CONFLICT (url) DO NOTHING", (novel_id, data.next_url))
    if data.translation:
        engine = data.translation_engine or "mbart50"
        cur.execute("""
            INSERT INTO chapter_translations (chapter_id, engine, content)
            VALUES (%s, %s, %s)
            ON CONFLICT (chapter_id, engine) DO UPDATE SET content = EXCLUDED.content
        """, (data.chapter_id, engine, data.translation))
    conn.commit()
    conn.close()
    return {"status": "success"}
