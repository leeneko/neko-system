import time
import random
import requests
import os
import sys
from DrissionPage import ChromiumPage, ChromiumOptions

# 설정
CONFIG_FILE = "config.txt"
DEFAULT_OCI_URL = "http://144.24.87.146:8001"
TRANSLATION_ENGINE = "mbart50"
TRANSLATION_URL = os.environ.get("TRANSLATION_URL", "http://127.0.0.1:5001/translate")
ENABLE_TRANSLATION = True


def load_server_url():
    url = DEFAULT_OCI_URL
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.startswith("http"):
                    url = content
        except Exception:
            pass
    return url


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
        print(f"   -> [저장 완료] {filename}")
        return True
    except Exception as e:
        print(f"   -> [저장 실패] {e}")
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


def translate_text(text: str) -> str:
    if not ENABLE_TRANSLATION:
        return ""
    try:
        res = requests.post(
            TRANSLATION_URL,
            json={"text": text, "source_lang": "ja_XX", "target_lang": "ko_KR"},
            timeout=300
        )
        if res.status_code == 200:
            data = res.json()
            return data.get("translated", "")
    except Exception as e:
        print(f"   -> [번역 실패] {e}")
    return ""


def wait_for_content(page, target_url, timeout_sec=180):
    start_time = time.time()
    is_loaded = False
    while time.time() - start_time < timeout_sec:
        if is_syosetu(target_url):
            if page.ele('css:div.p-novel__text'):
                is_loaded = True
                break
        else:
            if page.ele('#novel_content'):
                is_loaded = True
                break

        if "Just a moment" in page.title or "잠시만" in page.title:
            sys.stdout.write("\r   -> [🛡️] Cloudflare 대기 중... (직접 클릭해서 뚫으세요)   ")
            sys.stdout.flush()
        else:
            sys.stdout.write("\r   -> [⏳] 로딩 중...                                      ")
            sys.stdout.flush()
        time.sleep(1)

    print("")
    return is_loaded


def process_job(server_url, job_data, page):
    target_url = job_data['url']
    chapter_id = job_data['chapter_id']
    print(f"\n>> [작업] {target_url}")

    try:
        # [핵심] 현재 탭이 엉뚱한 곳이면 이동, 아니면 유지
        # 이미 뚫어놓은 페이지라면 굳이 get()을 호출해서 새로고침 하지 않음
        if target_url not in page.url:
            print("   -> 페이지 이동...")
            page.get(target_url)

        # [절대 대기] Cloudflare고 뭐고 일단 본문 뜰 때까지 기다림
        # 사람이 보고 있으니 필요하면 직접 뚫으면 됨
        print("   -> [대기] 본문 로딩 대기 중... (화면을 확인하세요)")

        is_loaded = wait_for_content(page, target_url)
        if not is_loaded:
            print("   -> [실패] 시간 초과. (페이지가 로딩되지 않았습니다)")
            return False

        # --- 데이터 파싱 ---
        if is_syosetu(target_url):
            title, content, next_url = parse_syosetu(page)
        else:
            title, content, next_url = parse_booktoki(page)

        print(f"   -> 제목: {title[:15]}...")
        save_to_txt(title, content)

        translation = ""
        if ENABLE_TRANSLATION and content and is_syosetu(target_url):
            print("   -> 번역 진행 중...")
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

        if res.status_code == 200:
            print("   -> [완료] 전송 성공")
            return True
        else:
            print(f"   -> [에러] 전송 실패 {res.status_code}")
            return False

    except Exception as e:
        print(f"   -> [오류] {e}")
        return False


def main():
    print("==========================================")
    print("   Rabbit Home Worker (Attach Mode)")
    print("==========================================")
    print(" [중요] 반드시 'ChromeStart.bat'로 크롬을 먼저 켜두세요!")

    server_url = load_server_url()

    # [핵심] 9222 포트로 열린 크롬에 연결
    co = ChromiumOptions()
    co.set_local_port(9222)

    try:
        page = ChromiumPage(co)
        print(f">> 브라우저 연결 성공! (OCI: {server_url})")
    except Exception:
        print(f"\n[치명적 오류] 실행 중인 크롬을 찾을 수 없습니다.")
        print("1. ChromeStart.bat를 먼저 실행했는지 확인하세요.")
        print("2. 기존 크롬 창을 모두 닫고 다시 시도하세요.")
        input("엔터를 누르면 종료합니다...")
        return

    while True:
        try:
            res = requests.get(f"{server_url}/worker/get", timeout=10)
            data = res.json()

            if data.get('exists'):
                if process_job(server_url, data, page):
                    # 성공 시 랜덤 대기
                    wait = random.uniform(3, 6)
                    print(f"   -> 다음 작업 대기... ({wait:.1f}초)")
                    time.sleep(wait)
                else:
                    print("   -> 실패. 5초 대기...")
                    time.sleep(5)
            else:
                print(".", end="", flush=True)
                time.sleep(3)

        except requests.exceptions.ConnectionError:
            print("\n[접속 불가] OCI 서버 확인 필요.")
            time.sleep(10)
        except Exception as e:
            print(f"\n[오류] {e}")
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
