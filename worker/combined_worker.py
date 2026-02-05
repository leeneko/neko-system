# ...existing code...
import os
import sys
import time
import json
import hashlib
import logging
import requests
import traceback
from typing import Optional

# optional: HTML parsing
from bs4 import BeautifulSoup

# DrissionPage may be used in original project; keep import but not required at top-level for exe.
try:
    from DrissionPage import ChromiumPage, ChromiumOptions
    HAS_DRISSION = True
except Exception:
    HAS_DRISSION = False

# Configuration
CONFIG_FILE = "config.txt"
DEFAULT_OCI_URL = "http://144.24.87.146:8001"
POLL_ENDPOINT = os.environ.get("OCI_POLL_ENDPOINT", "/api/next_task")
RESULT_ENDPOINT = os.environ.get("OCI_RESULT_ENDPOINT", "/api/task_result")
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "facebook/mbart-large-50-many-to-many-mmt")
ENABLE_TRANSLATION = os.environ.get("ENABLE_TRANSLATION", "1") == "1"
MAX_CHARS = int(os.environ.get("TRANSLATE_MAX_CHARS", "800"))

# Base dir: executable location when frozen, otherwise source dir
_base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_base_dir, "worker_error.log")
DOWNLOAD_DIR = os.path.join(_base_dir, "download")
ORIGINAL_DIR = os.path.join(DOWNLOAD_DIR, "original")
TRANSLATION_DIR = os.path.join(DOWNLOAD_DIR, "translation")
COMBINED_DIR = os.path.join(DOWNLOAD_DIR, "combined")

os.makedirs(ORIGINAL_DIR, exist_ok=True)
os.makedirs(TRANSLATION_DIR, exist_ok=True)
os.makedirs(COMBINED_DIR, exist_ok=True)

# Logging
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def log_error(msg: str):
    logging.error(msg)

def log_info(msg: str):
    logging.info(msg)

def log_exception(msg: str):
    logging.error(msg + "\n" + traceback.format_exc())

def load_server_url() -> str:
    url = DEFAULT_OCI_URL
    cfg = os.path.join(_base_dir, CONFIG_FILE)
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.startswith("http"):
                    url = content
        except Exception:
            log_exception("Failed to read config.txt")
    return url.rstrip("/")

# Lazy-loaded translation model globals
_tokenizer = None
_model = None
_device = None

def ensure_model_loaded() -> bool:
    """Lazy-load tokenizer/model. Return True if loaded."""
    global _tokenizer, _model, _device
    if _tokenizer is not None and _model is not None and _device is not None:
        return True
    try:
        import torch
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

        td = os.environ.get("TRANSLATION_DEVICE", "cpu").lower()
        if td == "auto":
            _device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            _device = "cuda" if (td == "cuda" and torch.cuda.is_available()) else "cpu"

        _tokenizer = AutoTokenizer.from_pretrained(TRANSLATE_MODEL)
        _model = AutoModelForSeq2SeqLM.from_pretrained(TRANSLATE_MODEL).to(_device)
        log_info(f"Translation model loaded on {_device}")
        return True
    except Exception:
        log_exception("Failed to load translation model")
        _tokenizer = None
        _model = None
        _device = None
        return False

def chunk_text(text: str, max_chars: int):
    parts = text.split("\n\n")
    chunks = []
    buf = ""
    for part in parts:
        candidate = part if not buf else buf + "\n\n" + part
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            buf = part
    if buf:
        chunks.append(buf)
    return chunks

def translate_chunk(text: str, source_lang: str, target_lang: str) -> str:
    if _tokenizer is None or _model is None or _device is None:
        return ""
    try:
        import torch
        _tokenizer.src_lang = source_lang
        encoded = _tokenizer(text, return_tensors="pt", truncation=True, max_length=1024)
        encoded = {k: v.to(_device) for k, v in encoded.items()}
        with torch.no_grad():
            generated = _model.generate(
                **encoded,
                forced_bos_token_id=_tokenizer.lang_code_to_id[target_lang],
                max_length=1024,
            )
        return _tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
    except Exception:
        log_exception("translate_chunk failed")
        return ""

def translate_text(text: str) -> str:
    if not ENABLE_TRANSLATION:
        return ""
    try:
        if not ensure_model_loaded():
            log_error("Translation skipped: model not available")
            return ""
        chunks = chunk_text(text, MAX_CHARS)
        outputs = []
        for chunk in chunks:
            outputs.append(translate_chunk(chunk, "ja_XX", "ko_KR"))
        return "\n\n".join(outputs)
    except Exception:
        log_exception("Translation failed")
        return ""

def safe_filename(s: str) -> str:
    s = s.strip().replace("/", "_").replace("\\", "_")
    if not s:
        s = "file"
    # limit length
    return s[:120]

def fetch_html(url: str, timeout: int = 30) -> Optional[str]:
    try:
        headers = {"User-Agent": "RabbitWorker/1.0"}
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception:
        log_exception(f"Failed to fetch url: {url}")
        return None

def extract_text_from_html(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        # try common article containers
        candidates = soup.select("article, #content, .content, .entry-content, .post-content")
        target = None
        for c in candidates:
            if c.get_text(strip=True):
                target = c
                break
        if target is None:
            # fallback to body
            target = soup.body or soup
        paragraphs = target.find_all(["p", "h1", "h2", "h3", "pre"])
        texts = []
        for p in paragraphs:
            t = p.get_text(separator=" ", strip=True)
            if t:
                texts.append(t)
        if texts:
            return "\n\n".join(texts)
        # fallback: plain text
        return soup.get_text(separator="\n", strip=True)
    except Exception:
        log_exception("extract_text_from_html failed")
        return ""

def save_text_file(directory: str, base: str, suffix: str, content: str) -> str:
    # generate deterministic filename
    h = hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
    name = f"{safe_filename(base)}_{h}{suffix}.txt"
    path = os.path.join(directory, name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
    except Exception:
        log_exception(f"Failed to save file: {path}")
        return ""

def process_task(task: dict, server_url: str):
    """
    Expected minimal task shape: { "id": "...", "url": "http://..." }
    """
    try:
        task_id = task.get("id", "")
        url = task.get("url")
        if not url:
            log_error(f"Task {task_id} missing url")
            return

        log_info(f"Processing task {task_id} url={url}")

        html = fetch_html(url)
        if not html:
            # report failure
            post_result(server_url, {"id": task_id, "status": "failed", "reason": "fetch_failed"})
            return

        title = ""
        try:
            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)
        except Exception:
            title = ""

        original_text = extract_text_from_html(html)
        if not original_text.strip():
            log_error(f"No extracted text for {url}")
            post_result(server_url, {"id": task_id, "status": "failed", "reason": "no_text"})
            return

        # save original
        orig_path = save_text_file(ORIGINAL_DIR, title or url, "_orig", original_text)

        # translate
        translated = ""
        if ENABLE_TRANSLATION:
            translated = translate_text(original_text)

        trans_path = ""
        if translated:
            trans_path = save_text_file(TRANSLATION_DIR, title or url, "_trans", translated)

        # combined (original + translation)
        combined_content = original_text
        if translated:
            combined_content = original_text + "\n\n==== TRANSLATION ====\n\n" + translated
        combined_path = save_text_file(COMBINED_DIR, title or url, "_combined", combined_content)

        # report success with artifact paths (relative)
        rel_orig = os.path.relpath(orig_path, _base_dir) if orig_path else ""
        rel_trans = os.path.relpath(trans_path, _base_dir) if trans_path else ""
        rel_comb = os.path.relpath(combined_path, _base_dir) if combined_path else ""

        result = {
            "id": task_id,
            "status": "done",
            "artifacts": {
                "original": rel_orig,
                "translation": rel_trans,
                "combined": rel_comb
            }
        }
        post_result(server_url, result)
        log_info(f"Task {task_id} completed. files: {rel_comb}")

    except Exception:
        log_exception("process_task failed")
        post_result(server_url, {"id": task.get("id", ""), "status": "failed", "reason": "exception"})

def post_result(server_url: str, payload: dict):
    try:
        url = server_url + RESULT_ENDPOINT
        headers = {"Content-Type": "application/json"}
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
        if r.status_code >= 400:
            log_error(f"post_result returned {r.status_code}: {r.text}")
        return r
    except Exception:
        log_exception("post_result failed")
        return None

def get_next_task(server_url: str) -> Optional[dict]:
    try:
        if POLL_ENDPOINT.startswith("http"):
            url = POLL_ENDPOINT
        else:
            url = server_url.rstrip("/") + "/" + POLL_ENDPOINT.lstrip("/")
        log_info(f"Polling next task from {url}")
        r = requests.get(url, timeout=30)

        if r.status_code == 204:
            return None
        if r.status_code == 200:
            try:
                j = r.json()
            except Exception:
                log_error(f"get_next_task: invalid JSON from {url}: {r.text[:1000]}")
                return None
            if isinstance(j, dict) and "task" in j:
                return j.get("task")
            return j

        log_error(f"get_next_task returned {r.status_code}: {r.text[:2000]}")

        alts = os.environ.get("OCI_POLL_ALTERNATIVES", "")
        if alts:
            for ep in [e.strip() for e in alts.split(",") if e.strip()]:
                if ep.startswith("http"):
                    alt_url = ep
                else:
                    alt_url = server_url.rstrip("/") + "/" + ep.lstrip("/")
                log_info(f"Trying alternative poll endpoint {alt_url}")
                try:
                    r2 = requests.get(alt_url, timeout=20)
                    if r2.status_code == 200:
                        try:
                            j2 = r2.json()
                        except Exception:
                            log_error(f"alt {alt_url} returned non-json: {r2.text[:1000]}")
                            continue
                        if isinstance(j2, dict) and "task" in j2:
                            return j2.get("task")
                        return j2
                    else:
                        log_error(f"alternative {alt_url} returned {r2.status_code}: {r2.text[:1000]}")
                except Exception:
                    log_exception(f"alternative {alt_url} request failed")
        return None
    except Exception:
        log_exception("get_next_task failed")
        return None

def main_loop(poll_interval: int = 10):
    server_url = load_server_url()
    log_info(f"Worker started. server_url={server_url} translate_enabled={ENABLE_TRANSLATION}")
    while True:
        try:
            task = get_next_task(server_url)
            if not task:
                time.sleep(poll_interval)
                continue
            process_task(task, server_url)
        except KeyboardInterrupt:
            log_info("Worker interrupted, exiting.")
            break
        except Exception:
            log_exception("Main loop unexpected error")
            time.sleep(poll_interval)

if __name__ == "__main__":
    try:
        main_loop()
    except Exception:
        log_exception("Fatal error in worker")
        sys.exit(1)
# ...existing code...