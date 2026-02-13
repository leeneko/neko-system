"""
================================================================================
🐇 Rabbit System - Combined Worker (combined_worker.py)
================================================================================

목적:
  - OCI 서버와 통신하며 소설 다운로드, 추출, 번역을 자동화하는 워커
  - 부트토키(booktoki), 소세츠(syosetu) 등 다양한 사이트 지원
  - Cloudflare/CAPTCHA 자동 우회 (DrissionPage 브라우저 자동화)
  - 프록시 로테이션 및 지수백오프를 이용한 안티매크로 방지
  - 라운드-로빈 방식으로 여러 소설 작업을 번갈아 처리

작동 흐름:
  1. OCI 서버에서 작업 조회 (get_next_task)
  2. URL 크롤링 (fetch_html) - 실패 시 브라우저 자동화 사용
  3. HTML에서 소설 본문 추출 (extract_text_from_html)
  4. 다음 챕터 링크 추출 (extract_next_url)
  5. 파일 저장 및 결과 전송 (post_result)
  6. 라운드-로빈으로 다음 소설 작업 처리
  ※ 번역은 translate_worker_ollama.py에서 별도로 처리

환경변수:
  - OCI_SERVER_URL: OCI 서버 주소 (예: http://144.24.87.146:8001)
  - PROXY_LIST: 프록시 리스트 (;로 구분, 예: http://p1:8080;http://p2:8080)
  - DRISSION_PORT: 드래싱페이지 Chrome 포트 (기본값: 9222)
  - DRISSION_MANUAL_WAIT: CAPTCHA 대기 시간(초) (기본값: 600)
  - WORKER_BUFFER_SIZE: 한 번에 미리 로드할 작업 수 (기본값: 10)

================================================================================
"""

import os
import sys
import time
import json
import logging
import traceback
import requests
import subprocess
import random
import re
from typing import Optional
from collections import deque

# ============================================================================
# 로깅 설정
# ============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Add file handler to write logs to error.log
try:
    # If running as a bundled executable (PyInstaller), prefer sys.executable directory
    if getattr(sys, 'frozen', False):
        base_for_logs = os.path.dirname(sys.executable)
    else:
        base_for_logs = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

    log_file = os.path.join(base_for_logs, "error.log")
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
# DrissionPage 선택적 설치 (브라우저 자동화 - Cloudflare 우회용)
# ============================================================================
# DrissionPage: Selenium 대비 빠르고 강력한 브라우저 자동화 라이브러리
# 없으면 requests만 사용하고 403/Cloudflare 감지 시 요청 실패

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
    HAS_DRISSION = True
except Exception:
    ChromiumPage = None
    ChromiumOptions = None
    HAS_DRISSION = False

# ============================================================================
# 전역 설정
# ============================================================================

# 기본 OCI 서버 주소
DEFAULT_OCI_URL = os.environ.get("OCI_SERVER_URL", "http://144.24.87.146:8001")

# 실행 파일 기본 경로 (PyInstaller EXE 또는 Python 스크립트)
_base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

# 프록시 리스트 (환경변수에서 ;로 구분된 프록시 목록)
PROXY_LIST = []
if os.environ.get("PROXY_LIST"):
    PROXY_LIST = [p.strip() for p in os.environ.get("PROXY_LIST", "").split(";") if p.strip()]

# 번역 설정 (translate_worker_ollama.py에서 별도로 처리)
ENABLE_TRANSLATION = False  # 번역은 별도 translate_worker_ollama.py에서 처리

# DrissionPage (브라우저) 설정
DRISSION_PORT = int(os.environ.get("DRISSION_PORT", "9222"))
BROWSER_PAGE = None  # 전역 브라우저 페이지 객체 (재사용)

# 워커 설정
_FAILURE_COUNTS = {}  # 작업별 실패 횟수 (재시도 지수백오프용)
_REPEAT_COUNTS = {}  # 작업별 반복 처리 카운트 (서버가 같은 챕터를 반복할 때 사용)
MAX_REPEAT = int(os.environ.get("WORKER_MAX_REPEAT", "3"))

# ============================================================================
# BeautifulSoup 설치 확인
# ============================================================================

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("❌ BeautifulSoup4 필수: pip install beautifulsoup4")
    sys.exit(1)

# ============================================================================
# 필수 함수: load_server_url()
# ============================================================================

def load_server_url():
    """
    OCI 서버 주소 로드
    
    우선순위:
    1. 환경변수 OCI_SERVER_URL
    2. 현재 디렉토리의 config.txt 파일
    3. 기본값 (DEFAULT_OCI_URL)
    
    반환값:
      - 서버 주소 (예: http://144.24.87.146:8001)
    """
    url = DEFAULT_OCI_URL
    
    # config.txt에서 읽기 (로컬 설정)
    config_file = os.path.join(_base_dir, "config.txt")
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.startswith("http"):
                    url = content
        except Exception as e:
            log_error(f"config.txt 읽기 실패: {e}")
    
    return url

# Worker endpoints (can be overridden via env vars)
GET_ENDPOINT = os.environ.get("OCI_GET_ENDPOINT", "/worker/get")
RESULT_ENDPOINT = os.environ.get("OCI_RESULT_ENDPOINT", "/worker/submit")
FAIL_ENDPOINT = os.environ.get("OCI_FAIL_ENDPOINT", "/worker/fail")

def _build_url(server_url: str, endpoint: str) -> str:
    if not server_url:
        return endpoint
    if server_url.endswith('/') and endpoint.startswith('/'):
        return server_url[:-1] + endpoint
    if not server_url.endswith('/') and not endpoint.startswith('/'):
        return server_url + '/' + endpoint
    return server_url + endpoint

def get_next_task(server_url: str) -> Optional[dict]:
    try:
        url = _build_url(server_url, GET_ENDPOINT)
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            log_error(f"get_next_task HTTP {r.status_code}")
            return None
        j = r.json()
        if not j:
            return None

        # Support responses like {"exists": True, "data": {...}} or direct payload
        payload = None
        if isinstance(j, dict) and j.get("exists") is True:
            payload = j.get("data") or j
        elif isinstance(j, dict) and ("chapter_id" in j or "id" in j) and "url" in j:
            payload = j

        if not payload:
            return None

        # Normalize fields
        chapter_id = payload.get("chapter_id") or payload.get("id")
        url = payload.get("url")
        novel_id = payload.get("novel_id") or payload.get("novelId") or payload.get("novel")

        if chapter_id and url:
            return {"id": chapter_id, "url": url, "novel_id": novel_id}

        return None
    except Exception:
        log_exception("get_next_task failed")
        return None

def post_result(server_url: str, payload: dict):
    try:
        url = _build_url(server_url, RESULT_ENDPOINT)
        body = {
            "chapter_id": payload.get("id") or payload.get("chapter_id"),
            "url": payload.get("url", ""),
            "title": payload.get("title", "") or payload.get("chapter_title", ""),
            "content": payload.get("content", "") or payload.get("body", ""),
            "next_url": payload.get("next_url") or payload.get("next"),
        }
        headers = {"Content-Type": "application/json"}
        r = requests.post(url, headers=headers, json=body, timeout=20)
        if r.status_code >= 400:
            log_error(f"post_result HTTP {r.status_code}")
        return r
    except Exception:
        log_exception("post_result failed")
        return None

def post_fail(server_url: str, payload: dict):
    try:
        url = _build_url(server_url, FAIL_ENDPOINT)
        headers = {"Content-Type": "application/json"}
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            log_error(f"post_fail HTTP {r.status_code}")
        return r
    except Exception:
        log_exception("post_fail failed")
        return None

# Chrome utility
def _find_chrome_executable():
    candidates = []
    if sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
        ]
    else:
        candidates = ["google-chrome", "chrome", "chromium", "chromium-browser"]
    for c in candidates:
        try:
            if sys.platform.startswith("win"):
                if os.path.exists(c):
                    return c
            else:
                p = subprocess.run(["which", c], capture_output=True, text=True)
                path = p.stdout.strip()
                if path:
                    return path
        except Exception:
            continue
    return None

def start_chrome_background(port: int = 9222, user_data_dir: str = None, headful: bool = True):
    exe = _find_chrome_executable()
    if not exe:
        log_error("Chrome/Chromium binary not found")
        return False
    if not user_data_dir:
        user_data_dir = os.path.join(_base_dir, "chrome_profile")
        os.makedirs(user_data_dir, exist_ok=True)
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={user_data_dir}", "--no-first-run"]
    if not headful:
        args.append("--headless")
    try:
        if sys.platform.startswith("win"):
            bat = os.path.join(_base_dir, "ChromeStart.bat")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(f'start "" "{exe}" {" ".join(args[1:])}\n')
            subprocess.Popen(["cmd", "/c", bat], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            return True
    except Exception:
        log_exception("start_chrome_background failed")
        return False

def init_drission_connection():
    global BROWSER_PAGE
    if not HAS_DRISSION:
        return
    try:
        co = ChromiumOptions()
        try:
            if hasattr(co, "set_local_port"):
                co.set_local_port(DRISSION_PORT)
            else:
                setattr(co, "local_port", DRISSION_PORT)
        except Exception:
            pass
        try:
            co.headless = False
        except Exception:
            pass

        # try attach first
        try:
            BROWSER_PAGE = ChromiumPage(co)
            return
        except Exception:
            pass

        # try to start Chrome then attach
        if not start_chrome_background(port=DRISSION_PORT, headful=True):
            BROWSER_PAGE = None
            return

        for _ in range(12):
            try:
                time.sleep(1)
                BROWSER_PAGE = ChromiumPage(co)
                return
            except Exception:
                continue

        log_error("Failed to attach to Chrome")
        BROWSER_PAGE = None
    except Exception:
        log_exception("init_drission_connection failed")
        BROWSER_PAGE = None

def fetch_html(url: str, timeout: int = 30) -> Optional[str]:
    status = None
    MANUAL_WAIT_SECONDS = int(os.environ.get("DRISSION_MANUAL_WAIT", "600"))
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "Referer": DEFAULT_OCI_URL,
            "Accept-Language": "en-US,en;q=0.9"
        }
        s = requests.Session()
        if PROXY_LIST:
            proxy = random.choice(PROXY_LIST)
            if proxy:
                s.proxies.update({"http": proxy, "https": proxy})
        r = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        status = getattr(r, "status_code", None)
        body_snip = getattr(r, "text", "")[:1200]
        if status == 200:
            return r.text
        log_error(f"Failed to fetch {url}: HTTP {status}")
        challenge_keywords = ("just a moment", "cf-challenge", "checking your browser", "cloudflare", "captcha")
        is_challenge = any(k in body_snip.lower() for k in challenge_keywords)
        if status != 403 and not is_challenge:
            return None
    except requests.exceptions.RequestException as e:
        try:
            status = e.response.status_code if getattr(e, "response", None) is not None else None
        except Exception:
            status = None
        log_error(f"Failed to fetch {url}: HTTP {status}")
        if status != 403:
            return None
        is_challenge = True

    if not (status == 403 or is_challenge):
        return None
    if not HAS_DRISSION:
        log_error("Cloudflare challenge and DrissionPage not available")
        return None

    page = None
    created_local = False
    try:
        if BROWSER_PAGE:
            page = BROWSER_PAGE
        else:
            opts = None
            try:
                opts = ChromiumOptions()
                try: opts.headless = False
                except Exception: pass
                try:
                    if hasattr(opts, "set_local_port"):
                        opts.set_local_port(DRISSION_PORT)
                    else:
                        setattr(opts, "local_port", DRISSION_PORT)
                except Exception:
                    pass
            except Exception:
                opts = None
            try:
                page = ChromiumPage(opts) if opts is not None else ChromiumPage()
                created_local = True
            except Exception:
                log_exception("Failed to create ChromiumPage")
                return None

        # navigate
        opened = False
        try:
            current_url = None
            if hasattr(page, "url"):
                current_url = getattr(page, "url")
            elif hasattr(page, "current_url"):
                current_url = getattr(page, "current_url")
            elif hasattr(page, "driver") and hasattr(page.driver, "current_url"):
                current_url = page.driver.current_url
            if current_url and url in current_url:
                opened = True
        except Exception:
            pass

        if not opened:
            for m in ("get","open","go","visit","navigate","goto"):
                if hasattr(page, m):
                    try:
                        getattr(page, m)(url)
                        opened = True
                        break
                    except Exception:
                        continue
        if not opened and hasattr(page, "driver") and hasattr(page.driver, "get"):
            try:
                page.driver.get(url)
                opened = True
            except Exception:
                pass
        if not opened:
            log_error("Browser failed to navigate to URL")
            return None

        start = time.time()
        html = None
        challenge_detected = False
        log_error(f"[Browser] Loading {url} with {MANUAL_WAIT_SECONDS}s timeout")
        while True:
            val = None
            getters = ("get_page_source","get_page_html","get_html","get_source","page_source","html","get_html_source","get_page")
            for g in getters:
                try:
                    if hasattr(page, g):
                        attr = getattr(page, g)
                        val = attr() if callable(attr) else attr
                        if isinstance(val, str) and val.strip():
                            break
                except Exception:
                    val = None
                    continue
            if not val and hasattr(page, "driver"):
                try:
                    val = page.driver.page_source
                except Exception:
                    val = None

            if isinstance(val, str) and val.strip():
                snippet = val[:2000].lower()
                challenge_keywords = ("just a moment","cf-challenge","checking your browser","cloudflare","captcha")
                has_challenge = any(k in snippet for k in challenge_keywords)
                
                if not has_challenge:
                    html = val
                    break
                else:
                    if not challenge_detected:
                        challenge_detected = True
                        log_error(f"[Browser] Challenge detected, waiting for completion...")
            
            elapsed = int(time.time() - start)
            if elapsed % 10 == 0:
                log_error(f"[Browser] Still waiting for {url}... {elapsed}s / {MANUAL_WAIT_SECONDS}s")
            if time.time() - start > MANUAL_WAIT_SECONDS:
                log_error(f"[Browser] Timeout after {MANUAL_WAIT_SECONDS}s for {url}")
                break
            time.sleep(3)

        return html
    finally:
        if created_local and page:
            try: page.quit()
            except Exception: pass

# simple extraction helper
def extract_text_from_html(html: str, url: str = "") -> str:
    """
    Extract novel content from HTML.
    Detects site type and applies appropriate parsing rules.
    
    Supports:
    - syosetu: <div class="js-novel-text p-novel__text"> with <p id="L1">, <p id="L2">, etc.
    - booktoki: <div id="novel_content"> with <p> tags
    - generic: fallback to multiple selectors
    """
    try:
        log_info(f"🔍 Starting HTML parsing for {url}...")
        soup = BeautifulSoup(html, "html.parser")
        log_info(f"   ✓ HTML parsed (size: {len(html)} bytes)")
        
        # Remove script and style elements
        for el in soup(['script', 'style']):
            el.decompose()
        
        # Cloudflare check
        text_content = soup.get_text()
        if "just a moment" in text_content.lower() or "cf-challenge" in text_content.lower():
            log_error("Detected Cloudflare challenge page")
            return None
        
        # Detect syosetu
        is_syosetu = "syosetu" in url.lower() or soup.select_one('article.p-novel') is not None
        
        if is_syosetu:
            log_info("📖 Detected syosetu format, using specialized parser")
            result = _extract_syosetu(soup)
        else:
            log_info("📚 Using generic parser (booktoki or other)")
            result = _extract_generic(soup)
        
        if result:
            log_info(f"✓ Extraction successful: {len(result)} characters extracted")
        else:
            log_error(f"❌ Extraction returned empty result")
        
        return result
            
    except Exception:
        log_exception("extract_text_from_html failed")
        return ""

def _extract_syosetu(soup) -> str:
    """
    Extract content from syosetu format.
    Looks for <div class="js-novel-text"> or <div class="js-novel-text p-novel__text"> containing <p id="L1">, <p id="L2">, etc.
    Filters out author notes (which may appear before story content).
    """
    try:
        log_info("   📖 [SYOSETU] Extracting with L-ID selector...")
        # Prefer any <p id="L###"> across the whole document (covers preface/main/afterword)
        ps_all = soup.find_all('p', id=True)
        log_info(f"      Found {len(ps_all)} <p> tags with id attribute")
        
        texts_with_ids = []
        all_ids = []
        for p in ps_all:
            p_id = (p.get('id') or '').strip()
            m = re.match(r'^L(\d+)$', p_id)
            if m:
                try:
                    line_num = int(m.group(1))
                    t = p.get_text()
                    if t:
                        texts_with_ids.append((line_num, t))
                        all_ids.append(line_num)
                except Exception:
                    continue

        if texts_with_ids:
            texts_with_ids.sort(key=lambda x: x[0])
            min_id = min(all_ids)
            max_id = max(all_ids)
            gap_count = max_id - min_id + 1 - len(texts_with_ids)
            log_info(f"      ✓ Found {len(texts_with_ids)} paragraphs with L-IDs (L{min_id}~L{max_id}, gap={gap_count})")
            return "\n".join([t for _, t in texts_with_ids])

        # Fallback: try to find the main text container (non-preface/non-afterword)
        log_info("   📖 [SYOSETU] L-ID method failed, trying container selector...")
        containers = soup.select('div.js-novel-text')
        log_info(f"      Found {len(containers)} js-novel-text containers")
        
        main_container = None
        for c in containers:
            cls = ' '.join(c.get('class') or [])
            if 'preface' in cls or 'afterword' in cls:
                continue
            main_container = c
            break
        if not main_container and containers:
            main_container = containers[0]

        if main_container is None:
            log_error("      ❌ Could not find any js-novel-text container for syosetu")
            return ""

        # Debug info
        all_ps = main_container.find_all('p', limit=20)
        id_samples = [p.get('id', 'NO_ID') for p in all_ps[:10]]
        text_samples = [p.get_text()[:80] for p in all_ps[:3]]
        log_info(f"      First 10 IDs: {id_samples}")
        log_info(f"      First 3 texts: {text_samples}")

        result = main_container.get_text("\n", strip=True)
        log_info(f"      ✓ Fallback extraction: {len(result)} characters")
        return result
            
    except Exception:
        log_exception("_extract_syosetu failed")
        return ""

def _extract_generic(soup) -> str:
    """
    Generic extraction for booktoki and other sites.
    Tries multiple CSS selectors.
    """
    try:
        log_info("   📚 [GENERIC] Trying CSS selectors...")
        el = None
        # Try booktoki specific selector first
        for sel in ['div#novel_content', '#novel_content', '.novel_content', 
                    'div.novel_content', '#content', '.content', 'div.content',
                    'div.reading-body', '.reading-body', 'article', 'main']:
            el = soup.select_one(sel)
            if el:
                log_info(f"      ✓ Found content with selector: {sel}")
                break
        
        if not el:
            log_info("      ⚠️ No CSS selector matched, fallback to full text")
            # fallback: largest text block
            result = soup.get_text("\n", strip=True)
            log_info(f"      Fallback result: {len(result)} characters")
            return result
        
        # Extract only p tags first (most reliable)
        ps = el.find_all('p', recursive=True)
        log_info(f"      Found {len(ps)} <p> tags in container")
        texts = []
        
        if ps:
            # If we found p tags, use only those
            for p in ps:
                t = p.get_text(separator=" ", strip=True)
                if t and len(t) > 5:  # Skip very short text
                    texts.append(t)
            log_info(f"      Extracted {len(texts)} paragraphs from <p> tags")
        else:
            log_info("      No <p> tags found, trying direct text nodes...")
            # Fallback: if no p tags, try direct text nodes in the element
            for child in el.children:
                if isinstance(child, str):
                    t = child.strip()
                    if t and len(t) > 5:
                        texts.append(t)
                elif child.name and child.name not in ['script', 'style']:
                    # For non-paragraph elements, extract text
                    t = child.get_text(separator=" ", strip=True)
                    if t and len(t) > 5:
                        texts.append(t)
            log_info(f"      Extracted {len(texts)} text nodes")
        
        if texts:
            result = "\n\n".join(texts)
            log_info(f"      ✓ Generic extraction: {len(result)} characters")
            return result
        else:
            log_info("      No texts found with any method, using full container text")
            result = el.get_text("\n", strip=True)
            log_info(f"      Full container text: {len(result)} characters")
            return result
            
    except Exception:
        log_exception("_extract_generic failed")
        return ""

def extract_next_url(html: str, url: str) -> str:
    """
    Extract next chapter URL from HTML.
    Detects site type and applies appropriate parsing rules.
    
    Supports:
    - booktoki: <a id="goNextBtn"> or <a class="btn btn-lg btn-default">
    - syosetu: <a class="c-pager__item c-pager__item--next"> (returns relative URL like /n3289ds/2/)
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        
        # Detect syosetu
        is_syosetu = "syosetu" in url.lower() or soup.select_one('article.p-novel') is not None
        
        if is_syosetu:
            return _extract_next_url_syosetu(soup, url)
        else:
            return _extract_next_url_booktoki(soup, url)
            
    except Exception:
        log_exception("extract_next_url failed")
        return ""

def _extract_next_url_booktoki(soup, url: str) -> str:
    """
    Extract next chapter URL from booktoki format.
    Tries: <a id="goNextBtn"> then <a class="btn btn-lg btn-default">
    """
    try:
        # Try id="goNextBtn" first
        next_link = soup.select_one('a#goNextBtn')
        if next_link:
            href = next_link.get('href', '')
            if href:
                log_info(f"Found next URL via goNextBtn: {href}")
                return href
        
        # Try class="btn btn-lg btn-default"
        # This might appear multiple times, so check for "next" or similar context
        links = soup.select('a.btn.btn-lg.btn-default')
        for link in links:
            href = link.get('href', '')
            text = link.get_text(strip=True).lower()
            # Look for "next" or "다음" in text
            if href and ('next' in text or '다음' in text or '이후' in text):
                log_info(f"Found next URL via btn-default: {href}")
                return href
        
        # If no luck, try last btn-default link (might be next button)
        if links:
            href = links[-1].get('href', '')
            if href:
                log_info(f"Using last btn-default as next URL: {href}")
                return href
        
        log_info("No next URL found for booktoki")
        return ""
        
    except Exception:
        log_exception("_extract_next_url_booktoki failed")
        return ""

def _extract_next_url_syosetu(soup, url: str) -> str:
    """
    Extract next chapter URL from syosetu format.
    Looks for <a class="c-pager__item c-pager__item--next">
    Returns relative URL like /n3289ds/2/
    Note: There are usually 2 (top and bottom), return first one
    """
    try:
        next_links = soup.select('a.c-pager__item.c-pager__item--next')
        
        if not next_links:
            log_info("📖 [SYOSETU] No next URL selector found - checking if this is the last chapter...")
            
            # Additional check: look for disabled/inactive next button
            disabled_next = soup.select('a.c-pager__item.c-pager__item--next[disabled]')
            if disabled_next:
                log_info("📖 [SYOSETU] Confirmed: Next button is disabled - this is the LAST CHAPTER")
                return ""
            
            # Check page structure for last chapter indicators
            page_info = soup.select_one('span.c-pager__num')
            if page_info:
                log_info(f"📖 [SYOSETU] Pager info: {page_info.get_text(strip=True)}")
            
            log_info("📖 [SYOSETU] This appears to be the final chapter (no next link found)")
            return ""
        
        # Get the first one (they appear at top and bottom)
        next_link = next_links[0]
        href = next_link.get('href', '')
        
        if href:
            log_info(f"✓ [SYOSETU] Found next URL: {href}")
            # The href should be relative like /n3289ds/2/
            # If it's already relative, return as-is
            if href.startswith('/'):
                return href
            # If it's full URL, extract the path
            if 'syosetu.com' in href:
                # Extract path from full URL
                from urllib.parse import urlparse
                parsed = urlparse(href)
                return parsed.path
            return href
        
        log_info("⚠️ [SYOSETU] Next link element found but no href attribute")
        return ""
        
    except Exception:
        log_exception("_extract_next_url_syosetu failed")
        return ""

def save_text_file(dirpath, title, suffix, content):
    try:
        os.makedirs(dirpath, exist_ok=True)
        safe = "".join([c for c in (title or "untitled") if c.isalnum() or c in (' ', '_','-')]).strip()[:120]
        fname = f"{safe}{suffix}.txt"
        path = os.path.join(dirpath, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
    except Exception:
        log_exception("save_text_file failed")
        return None

def sanitize_chapter_title(raw_title: str) -> str:
    title = (raw_title or "").strip()
    if not title:
        return "제목 없음"
    title = title.replace("::", ":")
    parts = [p.strip() for p in title.replace("|", " - ").split(" - ") if p.strip()]
    noise_tokens = ("booktoki", "북토끼", "syosetu", "소설가가되자")
    filtered = []
    for part in parts:
        if any(tok in part.lower() for tok in noise_tokens):
            continue
        if not filtered or filtered[-1] != part:
            filtered.append(part)
    cleaned = " - ".join(filtered) if filtered else title
    return cleaned[:180].strip()

# directories
ORIGINAL_DIR = os.path.join(_base_dir, "download", "original")

def process_task(task: dict, server_url: str):
    try:
        task_id = task.get("id", "")
        url = task.get("url")
        if not url:
            log_error(f"Task {task_id} missing url")
            return
        log_info(f"Processing task {task_id} url={url}")
        # Track how many times this task has been processed to avoid infinite loops
        try:
            cnt = _REPEAT_COUNTS.get(task_id, 0) + 1
            _REPEAT_COUNTS[task_id] = cnt
            if cnt > MAX_REPEAT:
                log_error(f"⚠️ Task {task_id} exceeded max repeat {MAX_REPEAT}, marking as failed and skipping")
                post_fail(server_url, {"chapter_id": task_id, "url": url, "reason": "max_repeat_exceeded"})
                return
        except Exception:
            pass
        html = fetch_html(url)
        if not html:
            log_error(f"Fetching failed for task {task_id} url={url} -> notifying server")
            post_fail(server_url, {"chapter_id": task_id, "url": url, "reason": "fetch_failed"})
            cnt = _FAILURE_COUNTS.get(task_id, 0) + 1
            _FAILURE_COUNTS[task_id] = cnt
            backoff = min(5 * cnt, 60)
            log_info(f"Task {task_id} backoff {backoff}s (failure_count={cnt})")
            time.sleep(backoff)
            return

        if task_id in _FAILURE_COUNTS:
            del _FAILURE_COUNTS[task_id]

        title = ""
        try:
            soup = BeautifulSoup(html, "html.parser")
            ttag = soup.find("title")
            if ttag:
                title = ttag.get_text(strip=True)
        except Exception:
            title = ""

        log_info(f"Extracting text from {url}...")
        original_text = extract_text_from_html(html, url)
        
        # If extraction returned None, it's a Cloudflare challenge page
        if original_text is None:
            log_error(f"Cloudflare challenge detected for {url}, will retry")
            post_fail(server_url, {"chapter_id": task_id, "url": url, "reason": "cloudflare_challenge"})
            cnt = _FAILURE_COUNTS.get(task_id, 0) + 1
            _FAILURE_COUNTS[task_id] = cnt
            backoff = min(5 * cnt, 60)
            log_info(f"Task {task_id} backoff {backoff}s due to cloudflare (failure_count={cnt})")
            time.sleep(backoff)
            return
        
        if not original_text.strip():
            log_error(f"❌ No extracted text for {url} (result was empty or only whitespace)")
            log_error(f"   original_text type: {type(original_text)}, length: {len(original_text) if original_text else 0}")
            post_fail(server_url, {"chapter_id": task_id, "url": url, "reason": "no_text"})
            return
        
        log_info(f"✓ Extracted {len(original_text)} characters from {url}")

        save_text_file(ORIGINAL_DIR, title or url, "_orig", original_text)

        # Extract next chapter URL
        log_info(f"Extracting next chapter URL from {url}...")
        extracted_next = extract_next_url(html, url)
        log_info(f"   Extracted next_url (raw): {extracted_next}")

        # Normalize next URL to absolute form and validate it to avoid looping
        from urllib.parse import urljoin, urlparse, urlunparse
        normalized_next = ""
        if extracted_next:
            try:
                if extracted_next.startswith('http'):
                    normalized_next = extracted_next
                else:
                    normalized_next = urljoin(url, extracted_next)
            except Exception:
                normalized_next = extracted_next

        # If this is a syosetu URL, force a full absolute URL with configured base domain
        try:
            is_syosetu_local = False
            try:
                is_syosetu_local = ('syosetu' in url.lower()) or (soup is not None and soup.select_one('article.p-novel') is not None)
            except Exception:
                is_syosetu_local = ('syosetu' in url.lower())

            if is_syosetu_local and normalized_next:
                # Force domain to configured SYOSETU_BASE (default ncode.syosetu.com)
                SYOSETU_BASE = os.environ.get('SYOSETU_BASE', 'https://ncode.syosetu.com')
                pnext = urlparse(normalized_next)
                # Build path+query
                path = pnext.path or ''
                query = ('?' + pnext.query) if pnext.query else ''
                # If the existing netloc is missing or not a syosetu domain, replace it
                try:
                    if not pnext.netloc or 'syosetu' not in pnext.netloc:
                        normalized_next = urljoin(SYOSETU_BASE, path + (('?' + pnext.query) if pnext.query else ''))
                        log_info(f"   Forced syosetu base domain for next: {normalized_next}")
                    else:
                        # Ensure scheme present
                        if not pnext.scheme:
                            pbase = urlparse(SYOSETU_BASE)
                            normalized_next = urlunparse((pbase.scheme, pnext.netloc, pnext.path, '', pnext.query, ''))
                            log_info(f"   Normalized syosetu next with scheme: {normalized_next}")
                except Exception:
                    # fallback: ensure absolute using SYOSETU_BASE
                    normalized_next = urljoin(SYOSETU_BASE, path + query)
        except Exception:
            pass

        # If normalized next equals current URL (same path), clear it to avoid reprocessing same chapter
        try:
            if normalized_next:
                cur_norm = urlparse(url).path or url
                next_norm = urlparse(normalized_next).path or normalized_next
                if next_norm.rstrip('/') == cur_norm.rstrip('/') or normalized_next.rstrip('/') == url.rstrip('/') or normalized_next == url:
                    log_info(f"⚠️ Normalized next URL equals current URL; clearing next_url to avoid loop: {normalized_next}")
                    normalized_next = ""
        except Exception:
            pass

        if not normalized_next:
            log_info(f"⚠️ No next chapter found or normalized next is empty - this may be the final chapter")
        else:
            log_info(f"✓ Next chapter URL (normalized): {normalized_next}")

        # 번역은 translate_worker_ollama.py에서 별도로 처리
        # combined_worker는 원본 텍스트만 추출하여 저장

        cleaned_title = sanitize_chapter_title(title)
        result = {
            "id": task_id,
            "chapter_id": task_id,
            "url": url,
            "title": cleaned_title,
            "content": original_text,
            # Send only normalized absolute next_url to server to avoid ambiguity
            "next_url": normalized_next
        }
        
        # Log completion status with clear formatting
        log_info(f"\n" + "="*70)
        log_info(f"✅ TASK COMPLETED: {task_id}")
        log_info(f"   Title: {cleaned_title}")
        log_info(f"   Original text: {len(original_text)} chars")
        log_info(f"   (번역은 translate_worker_ollama.py에서 별도로 처리됨)")
        if normalized_next:
            log_info(f"   Next chapter: {normalized_next}")
        else:
            log_info(f"   Status: 🏁 LAST CHAPTER (no next URL)")
        log_info(f"="*70 + "\n")
        
        # Send result to server and log response for debugging
        try:
            log_info(f"Posting result to server: id={result.get('id')} title={result.get('title')}")
            resp = post_result(server_url, result)
            if resp is None:
                log_error("post_result returned None (no response) - notifying server of failure")
                post_fail(server_url, {"chapter_id": task_id, "url": url, "reason": "post_result_no_response"})
            else:
                try:
                    status = getattr(resp, 'status_code', None)
                    text = getattr(resp, 'text', '')[:2000]
                    log_info(f"post_result HTTP {status}: {text}")
                    if status is None or status >= 400:
                        log_error(f"Server returned error for chapter {task_id}: HTTP {status}")
                        post_fail(server_url, {"chapter_id": task_id, "url": url, "reason": f"post_result_http_{status}", "response": text})
                    else:
                        # Successful submission — clear repeat counter for this chapter
                        try:
                            if task_id in _REPEAT_COUNTS:
                                del _REPEAT_COUNTS[task_id]
                                log_info(f"Cleared repeat counter for task {task_id} after successful post_result")
                        except Exception:
                            pass
                except Exception:
                    log_exception("Error reading post_result response")
        except Exception:
            log_exception("Unexpected error while posting result")
    except Exception:
        log_exception("process_task failed")
        post_fail(server_url, {"chapter_id": task.get("id",""), "url": task.get("url",""), "reason":"exception"})

def main():
    server_url = load_server_url()
    init_drission_connection()
    log_info("Worker started")

    # Buffer size controls how many tasks we prefetch into per-novel queues
    buffer_size = int(os.environ.get("WORKER_BUFFER_SIZE", "10"))
    log_info(f"Using per-novel buffer_size={buffer_size} and sequential round-robin processing")

    # Mapping: novel_id -> deque(tasks)
    task_queues = {}
    # Rotation order (list of novel_id)
    rotation = []
    rot_idx = 0

    try:
        while True:
            try:
                # Pre-fill buffer up to buffer_size
                total_buffered = sum(len(q) for q in task_queues.values())
                while total_buffered < buffer_size:
                    task = get_next_task(server_url)
                    if not task:
                        break
                    novel_id = task.get("novel_id") or str(task.get("url") or "unknown_novel")
                    novel_id = str(novel_id)
                    if novel_id not in task_queues:
                        task_queues[novel_id] = deque()
                        rotation.append(novel_id)
                        log_info(f"📚 NEW NOVEL QUEUE: {novel_id}")
                    task_queues[novel_id].append(task)
                    total_buffered += 1
                    queue_status = " | ".join([f"{nid}: {len(q)}" for nid, q in task_queues.items()])
                    log_info(f"📥 Buffered task {task.get('id')} for novel {novel_id} | Queues: [{queue_status}]")

                # If no buffered tasks, wait briefly and retry
                if not rotation:
                    log_info("⏸️  No novels in rotation, waiting for new tasks...")
                    time.sleep(2)
                    continue

                # Select next novel in rotation
                if rot_idx >= len(rotation):
                    rot_idx = 0
                current_novel = rotation[rot_idx]
                rotation_str = " → ".join(rotation)
                log_info(f"🔁 ROTATION ORDER: [{rotation_str}] | Current: {current_novel} (index {rot_idx})")

                # If queue empty (possible due to removal), advance
                if current_novel not in task_queues or not task_queues[current_novel]:
                    log_info(f"⚠️  Queue for {current_novel} is empty, removing from rotation")
                    # remove from rotation
                    try:
                        rotation.pop(rot_idx)
                    except Exception:
                        rot_idx = (rot_idx + 1) % (len(rotation) or 1)
                    continue

                task = task_queues[current_novel].popleft()
                if not task_queues[current_novel]:
                    # remove empty queue from rotation
                    task_queues.pop(current_novel, None)
                    rotation.pop(rot_idx)
                else:
                    rot_idx = (rot_idx + 1) % len(rotation)

                task_id = task.get("id", "")
                log_info(f"🔄 ROUND-ROBIN PROCESSING: Task {task_id} (Novel: {current_novel})")
                process_task(task, server_url)

                # Small random delay between tasks to avoid macro detection
                # Reduced from 5-12s to 1-3s for faster processing
                jitter = random.uniform(1.0, 3.0)
                log_info(f"⏳ Waiting {jitter:.1f}s before next task...")
                time.sleep(jitter)

            except KeyboardInterrupt:
                log_info("Keyboard interrupt, exiting main loop")
                break
            except Exception:
                log_exception("Main loop error")
                time.sleep(5)
    finally:
        log_info("Worker shutting down")


if __name__ == "__main__":
    main()
