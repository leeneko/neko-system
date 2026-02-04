import os
import sys
import time
import random
import logging
import subprocess
import requests
from DrissionPage import ChromiumPage, ChromiumOptions

import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

# 설정
CONFIG_FILE = "config.txt"
DEFAULT_OCI_URL = "http://144.24.87.146:8001"
TRANSLATION_ENGINE = "mbart50"
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "facebook/mbart-large-50-many-to-many-mmt")
ENABLE_TRANSLATION = os.environ.get("ENABLE_TRANSLATION", "1") == "1"
MAX_CHARS = int(os.environ.get("TRANSLATE_MAX_CHARS", "800"))

LOG_FILE = "worker_error.log"
logging.basicConfig(filename=LOG_FILE, level=logging.ERROR)

_tokenizer = None
_model = None
_device = "cuda" if torch.cuda.is_available() else "cpu"


def log_error(msg: str):
    logging.error(msg)


def log_exception(msg: str):
    logging.exception(msg)


def load_server_url():
    url = DEFAULT_OCI_URL
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.startswith("http"):
                    url = content
        except Exception:
            log_exception("Failed to read config.txt")
    return url


def start_chrome_if_needed():
    chrome_script = os.environ.get("CHROME_START", "ChromeStart.bat")
    if not os.path.exists(chrome_script):
        return
    try:
        if os.name == "nt":
            subprocess.Popen(
                ["cmd", "/c", chrome_script],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            subprocess.Popen([chrome_script])
    except Exception:
        log_exception("Failed to start ChromeStart.bat")


def save_to_txt(title, content):
    try:
        base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(
            os.path.abspath(__file__)
        )
        download_dir = os.path.join(base_dir, "download")
        if not os.path.exists(download_dir):
            os.makedirs(download_dir)

        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '_', '-', '(', ')', '[', ']')]).strip()
        filename = f"{safe_title}.txt"
        filepath = os.path.join(download_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"제목: {title}\n")
            f.write("=" * 40 + "\n\n")
            f.write(content)
        return True
    except Exception:
        log_exception("Failed to save txt")
        return False


def is_syosetu(url: str) -> bool:
    return "ncode.syosetu.com" in url


def parse_booktoki(page):
    title = page.title
    content = ""
    try:
        content_elem = page.ele('#novel_content')
        ps = content_elem.eles('tag:p')
        if ps:
            content = "\n".join([p.text for p in ps if p.text.strip()])
        else:
            content = content_elem.text
    except Exception:
        content = "본문 파싱 실패"

    next_url = ""
    try:
        btn = page.ele('#goNextBtn')
        if btn:
            href = btn.attr('href')
            if href and "http" in href:
                next_url = href.split('?')[0]
    except Exception:
        pass
    return title, content, next_url


def parse_syosetu(page):
    title = ""
    content = ""
    next_url = ""

    try:
        title_elem = page.ele('css:h1.p-novel__title')
        if title_elem:
            title = title_elem.text
    except Exception:
        pass
    if not title:
        title = page.title

    try:
        text_blocks = page.eles('css:div.p-novel__text')
        lines = []
        for block in text_blocks:
            ps = block.eles('tag:p')
            if ps:
                for p in ps:
                    lines.append(p.text)
            else:
                lines.append(block.text)
        content = "\n".join([line for line in lines if line is not None])
    except Exception:
        content = "본문 파싱 실패"

    try:
        btn = page.ele('css:a.c-pager__item--next')
        if btn:
            href = btn.attr('href')
            if href:
                if href.startswith("/"):
                    next_url = "https://ncode.syosetu.com" + href
                else:
                    next_url = href
    except Exception:
        pass

    return title, content, next_url


def wait_for_content(page, target_url, timeout_sec=180):
    start_time = time.time()
    while time.time() - start_time < timeout_sec:
        if is_syosetu(target_url):
            if page.ele('css:div.p-novel__text'):
                return True
        else:
            if page.ele('#novel_content'):
                return True
        time.sleep(1)
    return False


def ensure_model_loaded():
    global _tokenizer, _model
    if _tokenizer is None or _model is None:
        _tokenizer = AutoTokenizer.from_pretrained(TRANSLATE_MODEL)
        _model = AutoModelForSeq2SeqLM.from_pretrained(TRANSLATE_MODEL).to(_device)


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


def translate_text(text: str) -> str:
    if not ENABLE_TRANSLATION:
        return ""
    try:
        ensure_model_loaded()
        chunks = chunk_text(text, MAX_CHARS)
        outputs = []
        for chunk in chunks:
            outputs.append(translate_chunk(chunk, "ja_XX", "ko_KR"))
        return "\n\n".join(outputs)
    except Exception:
        log_exception("Translation failed")
        return ""


def process_job(server_url, job_data, page):
    target_url = job_data['url']
    chapter_id = job_data['chapter_id']

    try:
        if target_url not in page.url:
            page.get(target_url)

        is_loaded = wait_for_content(page, target_url)
        if not is_loaded:
            log_error("Timeout waiting for page load")
            return False

        if is_syosetu(target_url):
            title, content, next_url = parse_syosetu(page)
        else:
            title, content, next_url = parse_booktoki(page)

        save_to_txt(title, content)

        translation = ""
        if ENABLE_TRANSLATION and content and is_syosetu(target_url):
            translation = translate_text(content)

        payload = {
            "chapter_id": chapter_id,
            "content": content,
            "next_url": next_url,
            "title": title,
            "url": target_url
        }
        if translation:
            payload["translation"] = translation
            payload["translation_engine"] = TRANSLATION_ENGINE

        res = requests.post(f"{server_url}/worker/submit", json=payload)
        return res.status_code == 200

    except Exception:
        log_exception("process_job failed")
        return False


def main():
    server_url = load_server_url()
    start_chrome_if_needed()

    co = ChromiumOptions()
    co.set_local_port(9222)

    try:
        page = ChromiumPage(co)
    except Exception:
        log_error("Failed to connect to Chrome (port 9222). Ensure ChromeStart.bat is running.")
        return

    while True:
        try:
            res = requests.get(f"{server_url}/worker/get", timeout=10)
            data = res.json()

            if data.get('exists'):
                if process_job(server_url, data, page):
                    wait = random.uniform(3, 6)
                    time.sleep(wait)
                else:
                    time.sleep(5)
            else:
                time.sleep(3)

        except requests.exceptions.ConnectionError:
            time.sleep(10)
        except Exception:
            log_exception("Main loop error")
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
