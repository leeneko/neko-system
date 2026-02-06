import os
import sys
import time
import json
import logging
import traceback
import requests
import subprocess
import random
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
# logging helpers
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
def log_info(msg): logging.info(msg)
def log_error(msg): logging.error(msg)
def log_exception(msg): logging.exception(msg)

# DrissionPage optional
try:
    from DrissionPage import ChromiumPage, ChromiumOptions
    HAS_DRISSION = True
except Exception:
    ChromiumPage = None
    ChromiumOptions = None
    HAS_DRISSION = False
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
                        task_queues[novel_id].append(task)
                        total_buffered += 1
                        log_info(f"Buffered task {task.get('id')} for novel {novel_id} (total_buffered={total_buffered})")

                    # If no buffered tasks, wait briefly and retry
                    if not rotation:
                        time.sleep(2)
                        continue

                    # Select next novel in rotation
                    if rot_idx >= len(rotation):
                        rot_idx = 0
                    current_novel = rotation[rot_idx]

                    # If queue empty (possible due to removal), advance
                    if current_novel not in task_queues or not task_queues[current_novel]:
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
                    log_info(f"Processing task {task_id} from novel {current_novel} (round-robin)")
                    process_task(task, server_url)

                    # Small random delay between tasks to avoid macro detection
                    jitter = random.uniform(5.0, 12.0)
                    time.sleep(jitter)

                except KeyboardInterrupt:
                    log_info("Keyboard interrupt, exiting main loop")
                    break
                except Exception:
                    log_exception("Main loop error")
                    time.sleep(5)
        finally:
            log_info("Worker shutting down")


                if "chapter_id" in j and "url" in j:
                    return {"id": j.get("chapter_id"), "url": j.get("url"), "novel_id": j.get("novel_id")}
        else:
            log_error(f"get_next_task returned {r.status_code}: {r.text[:1000]}")
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
            "translation": payload.get("translation", ""),
            "translation_engine": payload.get("translation_engine", TRANSLATION_ENGINE),
        }
        headers = {"Content-Type": "application/json"}
        r = requests.post(url, headers=headers, json=body, timeout=20)
        if r.status_code >= 400:
            log_error(f"post_result returned {r.status_code}: {r.text}")
        else:
            log_info(f"post_result success: {r.status_code}")
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
            log_error(f"post_fail returned {r.status_code}: {r.text}")
        else:
            log_info(f"post_fail success: {r.status_code}")
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
        log_error("No chrome/chromium binary found to start")
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
            log_info("Started Chrome via ChromeStart.bat")
            return True
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            log_info(f"Started Chrome background: {exe} --remote-debugging-port={port}")
            return True
    except Exception:
        log_exception("start_chrome_background failed")
        return False

def init_drission_connection():
    global BROWSER_PAGE
    if not HAS_DRISSION:
        log_info("DrissionPage not installed; skipping browser attach")
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
            log_info(f"DrissionPage connected to Chrome on port {DRISSION_PORT}")
            return
        except Exception:
            log_info("No existing Chrome on port, will try to start one")

        # try to start Chrome then attach
        if not start_chrome_background(port=DRISSION_PORT, headful=True):
            BROWSER_PAGE = None
            return

        for _ in range(12):
            try:
                time.sleep(1)
                BROWSER_PAGE = ChromiumPage(co)
                log_info(f"DrissionPage attached after starting Chrome on port {DRISSION_PORT}")
                return
            except Exception:
                continue

        log_error("Failed to attach to Chrome after starting it")
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
        # If proxies are configured via PROXY_LIST env var, pick a random one
        if PROXY_LIST:
            proxy = random.choice(PROXY_LIST)
            if proxy:
                s.proxies.update({"http": proxy, "https": proxy})
                log_info(f"Using proxy for fetch: {proxy}")
        r = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        status = getattr(r, "status_code", None)
        body_snip = getattr(r, "text", "")[:1200]
        if status == 200:
            return r.text
        log_error(f"Failed to fetch url: {url} (HTTP {status}) body={body_snip[:800]}")
        challenge_keywords = ("just a moment", "cf-challenge", "checking your browser", "cloudflare", "captcha")
        is_challenge = any(k in body_snip.lower() for k in challenge_keywords)
        if status != 403 and not is_challenge:
            return None
    except requests.exceptions.RequestException as e:
        try:
            status = e.response.status_code if getattr(e, "response", None) is not None else None
        except Exception:
            status = None
        log_error(f"Failed to fetch url: {url} (HTTP {status})")
        if status != 403:
            log_exception(f"requests exception for {url}")
            return None
        is_challenge = True

    if not (status == 403 or is_challenge):
        return None
    if not HAS_DRISSION:
        log_error("403/challenge and no DrissionPage; skipping browser attempt")
        return None

    page = None
    created_local = False
    try:
        # prefer persistent BROWSER_PAGE
        if BROWSER_PAGE:
            page = BROWSER_PAGE
        else:
            # create temporary options
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
            # try multiple constructors
            try:
                if opts is not None:
                    page = ChromiumPage(opts)
                else:
                    page = ChromiumPage()
                created_local = True
            except Exception:
                log_exception("Failed to create temporary ChromiumPage")
                return None

        # navigate
        opened = False
        # If persistent page already at or containing the URL, reuse it (avoid reload)
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
                opened = False
        if not opened:
            log_error("Browser opened but couldn't navigate to URL")
            return None

        start = time.time()
        html = None
        challenge_detected = False
        log_info(f"Starting browser page source retrieval for {url} with {MANUAL_WAIT_SECONDS}s timeout")
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
                except Exception as e:
                    val = None
                    continue
            if not val and hasattr(page, "driver"):
                try:
                    drv = page.driver
                    if hasattr(drv, "page_source"):
                        val = drv.page_source
                except Exception as e:
                    val = None

            if isinstance(val, str) and val.strip():
                snippet = val[:2000].lower()
                challenge_keywords = ("just a moment","cf-challenge","checking your browser","cloudflare","captcha")
                has_challenge = any(k in snippet for k in challenge_keywords)
                
                if not has_challenge:
                    log_info(f"Page loaded successfully (no challenge detected)")
                    html = val
                    break
                else:
                    if not challenge_detected:
                        challenge_detected = True
                        log_info(f"Challenge/captcha detected. Waiting for user to complete it...")
            
            elapsed = int(time.time() - start)
            if elapsed % 10 == 0:
                status_msg = "Challenge/captcha still detected" if challenge_detected else "Waiting for page load"
                log_info(f"{status_msg} for {url}: elapsed {elapsed}s (up to {MANUAL_WAIT_SECONDS}s)")
            if time.time() - start > MANUAL_WAIT_SECONDS:
                if challenge_detected:
                    log_error(f"Challenge timeout after {MANUAL_WAIT_SECONDS}s for {url}. User did not complete captcha in time.")
                else:
                    log_error(f"Page load timeout after {MANUAL_WAIT_SECONDS}s for {url}")
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
        soup = BeautifulSoup(html, "html.parser")
        
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
            log_info("Detected syosetu format, using specialized parser")
            return _extract_syosetu(soup)
        else:
            log_info("Using generic parser (booktoki or other)")
            return _extract_generic(soup)
            
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
        # Find the novel text container
        text_container = soup.select_one('div.js-novel-text')
        
        if not text_container:
            log_error("Could not find syosetu js-novel-text container")
            return ""
        
        log_info(f"Found syosetu text container, extracting paragraphs with id=L*")
        
        # Extract ONLY paragraphs with id starting with "L" followed by digits (L1, L2, L3, etc.)
        ps = text_container.find_all('p', id=True)
        all_ids = []
        texts_with_ids = []
        
        for p in ps:
            p_id = p.get('id', '')
            
            # Match pattern like L1, L2, ..., L9999
            if p_id and p_id.startswith('L'):
                try:
                    line_num = int(p_id[1:])
                    t = p.get_text()
                    if t:
                        texts_with_ids.append((line_num, t))
                        all_ids.append(line_num)
                except (ValueError, IndexError):
                    pass
        
        if not texts_with_ids:
            log_error("No paragraphs with L-id found in div.js-novel-text")
            
            # Debug: Check what other p-tags exist
            all_ps = text_container.find_all('p', limit=20)
            id_samples = [p.get('id', 'NO_ID') for p in all_ps[:10]]
            text_samples = [p.get_text()[:50] for p in all_ps[:3]]
            log_error(f"DEBUG: Found {len(all_ps)} total <p> tags")
            log_error(f"DEBUG: First 10 IDs: {id_samples}")
            log_error(f"DEBUG: First 3 texts: {text_samples}")
            
            return text_container.get_text("\n", strip=True)
        
        # Sort by line number and join
        texts_with_ids.sort(key=lambda x: x[0])
        min_id = min(all_ids)
        max_id = max(all_ids)
        gap_count = max_id - min_id + 1 - len(texts_with_ids)
        
        log_info(f"Extracted {len(texts_with_ids)} paragraphs with L-IDs from syosetu (L{min_id} ~ L{max_id}, gap={gap_count})")
        
        # Join with single newline to preserve original formatting
        return "\n".join([t for _, t in texts_with_ids])
            
    except Exception:
        log_exception("_extract_syosetu failed")
        return ""

def _extract_generic(soup) -> str:
    """
    Generic extraction for booktoki and other sites.
    Tries multiple CSS selectors.
    """
    try:
        el = None
        # Try booktoki specific selector first
        for sel in ['div#novel_content', '#novel_content', '.novel_content', 
                    'div.novel_content', '#content', '.content', 'div.content',
                    'div.reading-body', '.reading-body', 'article', 'main']:
            el = soup.select_one(sel)
            if el:
                log_info(f"Found content with selector: {sel}")
                break
        
        if not el:
            # fallback: largest text block
            return soup.get_text("\n", strip=True)
        
        # Extract only p tags first (most reliable)
        ps = el.find_all('p', recursive=True)
        texts = []
        
        if ps:
            # If we found p tags, use only those
            for p in ps:
                t = p.get_text(separator=" ", strip=True)
                if t and len(t) > 5:  # Skip very short text
                    texts.append(t)
        else:
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
        
        if texts:
            return "\n\n".join(texts)
        else:
            return el.get_text("\n", strip=True)
            
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
            log_info("No next URL found for syosetu")
            return ""
        
        # Get the first one (they appear at top and bottom)
        next_link = next_links[0]
        href = next_link.get('href', '')
        
        if href:
            log_info(f"Found next URL via c-pager__item--next: {href}")
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
        
        log_info("No href found in next link")
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

# directories
ORIGINAL_DIR = os.path.join(_base_dir, "download", "original")
TRANSLATION_DIR = os.path.join(_base_dir, "download", "translation")
COMBINED_DIR = os.path.join(_base_dir, "download", "combined")

def process_task(task: dict, server_url: str):
    try:
        task_id = task.get("id", "")
        url = task.get("url")
        if not url:
            log_error(f"Task {task_id} missing url")
            return
        log_info(f"Processing task {task_id} url={url}")
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
            log_error(f"No extracted text for {url}")
            post_fail(server_url, {"chapter_id": task_id, "url": url, "reason": "no_text"})
            return

        save_text_file(ORIGINAL_DIR, title or url, "_orig", original_text)

        # Extract next chapter URL
        next_url = extract_next_url(html, url)

        translated = ""
        if ENABLE_TRANSLATION:
            # Attempt to translate using HuggingFace pipeline if available.
            try:
                from transformers import pipeline
                log_info(f"Attempting translation with model {TRANSLATE_MODEL}")
                translator = pipeline("translation", model=TRANSLATE_MODEL)
                parts = [original_text[i:i+MAX_CHARS] for i in range(0, len(original_text), MAX_CHARS)]
                translated_parts = []
                for p in parts:
                    try:
                        out = translator(p)
                        # Typical output: [{'translation_text': '...'}]
                        if isinstance(out, list) and len(out) > 0:
                            first = out[0]
                            if isinstance(first, dict) and 'translation_text' in first:
                                translated_parts.append(first['translation_text'])
                            elif isinstance(first, dict) and 'label' in first:
                                translated_parts.append(first.get('label',''))
                            else:
                                translated_parts.append(str(first))
                        elif isinstance(out, str):
                            translated_parts.append(out)
                        else:
                            translated_parts.append(str(out))
                    except Exception:
                        log_exception("translation chunk failed")
                        translated_parts.append("")
                translated = "\n\n".join([t for t in translated_parts if t])
            except Exception:
                log_error("Translation model not available; skipping translation")
                translated = ""

        if translated:
            save_text_file(TRANSLATION_DIR, title or url, "_trans", translated)

        combined_content = original_text + ("\n\n==== TRANSLATION ====\n\n" + translated if translated else "")
        combined_path = save_text_file(COMBINED_DIR, title or url, "_combined", combined_content)

        result = {
            "id": task_id,
            "chapter_id": task_id,
            "url": url,
            "title": title,
            "content": combined_content,
            "next_url": next_url
        }
        post_result(server_url, result)
        log_info(f"Task {task_id} completed. files: {combined_path}")
    except Exception:
        log_exception("process_task failed")
        post_fail(server_url, {"chapter_id": task.get("id",""), "url": task.get("url",""), "reason":"exception"})

def main():
    server_url = load_server_url()
    init_drission_connection()
    log_info("Worker started")
    
    # 환경변수로 워커 스레드 수 설정 (기본값: 3)
    max_workers = int(os.environ.get("WORKER_THREADS", "3"))
    log_info(f"Starting with {max_workers} parallel worker threads")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}  # future -> task_id mapping for tracking
        
        while True:
            try:
                # 완료된 future 처리 및 정리
                done_futures = []
                for future in list(futures.keys()):
                    if future.done():
                        try:
                            future.result()  # Get result to catch any exceptions
                        except Exception as e:
                            log_error(f"Task {futures[future]} raised exception: {e}")
                        done_futures.append(future)
                        del futures[future]
                
                # 활성 task 개수 확인
                active_count = len(futures)
                
                # 워커 스레드가 모두 사용 중이 아니면 새 task 폴링
                if active_count < max_workers:
                    task = get_next_task(server_url)
                    if task:
                        task_id = task.get("id", "unknown")
                        log_info(f"Submitting task {task_id} to thread pool (active: {active_count}/{max_workers})")
                        future = executor.submit(process_task, task, server_url)
                        futures[future] = task_id
                    else:
                        # No new task available, wait a bit
                        time.sleep(1)
                else:
                    # All workers busy, wait before polling again
                    log_info(f"All {max_workers} workers busy, waiting for task completion")
                    time.sleep(2)
                    
            except KeyboardInterrupt:
                log_info("Keyboard interrupt, waiting for active tasks to complete...")
                # Wait for remaining tasks to complete
                for future in futures.keys():
                    try:
                        future.result(timeout=300)
                    except Exception:
                        pass
                break
            except Exception:
                log_exception("Main loop error")
                time.sleep(5)

if __name__ == "__main__":
    main()