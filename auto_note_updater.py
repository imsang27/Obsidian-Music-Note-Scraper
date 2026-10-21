import os
import sys
import subprocess
import threading
import queue
from pathlib import Path
from note_format import ready, song_title, render_note, atomic_save, extract_property, extract_section
from playlist_queue import configured_queue, playlist_menu
from dotenv import load_dotenv

import time
import re
import requests
import urllib.parse
from bs4 import BeautifulSoup
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from google import genai
from ddgs import DDGS

from prompt import get_search_query_prompt, get_music_analysis_prompt, get_lyrics_only_prompt

# .env 파일 불러오기
load_dotenv()

# --- 1. API 설정 ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# .env에서 모델 목록을 가져와서 쉼표 기준으로 나눈 뒤 리스트(FALLBACK_MODELS)로 만듭니다.
# (만약 .env에 비워져 있다면 기본값으로 두 모델을 세팅합니다)
fallback_models_str = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite")
FALLBACK_MODELS = [model.strip() for model in fallback_models_str.split(",") if model.strip()]

client = None
stop_event = threading.Event()


def generate_text(prompt, model):
    global client
    if client is None:
        if not GEMINI_API_KEY or not FALLBACK_MODELS:
            raise ValueError("GEMINI_API_KEY와 GEMINI_FALLBACK_MODELS 설정을 확인하세요.")
        client = genai.Client(api_key=GEMINI_API_KEY, http_options={"timeout": 60000})
    return client.models.generate_content(model=model, contents=prompt)


# --- 1-1. API 대기 시간 추출 헬퍼 함수 ---
def get_retry_delay(error_msg, default_delay=60):
    """에러 메시지에서 'retry in OOs'를 찾아 대기 시간을 계산합니다."""
    delay_match = re.search(r'retry in ([\d\.]+)s', error_msg)
    if delay_match:
        return int(float(delay_match.group(1))) + 2 # 넉넉하게 2초 추가
    return default_delay

# --- 2. 원곡 정보 추출 함수 ---
def get_search_query(song_title):
    print(f"  -> 🔎 [진행 중] '{song_title}'에서 원곡 정보를 파악하는 중...")
    
    # 프롬프트를 외부 파일에서 불러옴
    prompt = get_search_query_prompt(song_title)

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        error_msg = "" # 에러 메시지 초기화
        for current_model in FALLBACK_MODELS:
            try:
                response = generate_text(prompt, current_model)
                query = response.text.strip()
                print(f"  -> 🎯 [완료] 검색어 확정: {query} (사용 모델: {current_model})")
                return query
            except Exception as e:
                error_msg = str(e)
                if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                    print(f"    - ⚠️ [{current_model}] 한도 초과. 즉시 다음 모델로 전환합니다.")
                    continue # 바로 다음 모델로 넘어감
                else:
                    print(f"  -> ❌ 원곡 추출 중 오류 발생: {error_msg}")
                    continue
        # 리스트에 있는 모든 모델이 실패했을 경우에만 대기
        if attempt < max_retries:
            delay = get_retry_delay(error_msg)
            print(f"  -> ⏳ [API 대기] 모든 모델 호출 실패. {delay}초 후 처음부터 재시도합니다...")
            stop_event.wait(delay)
            if stop_event.is_set():
                return None

    print("  -> ❌ [실패] 원곡 추출 재시도 횟수 초과.")
    return song_title

# --- 3. 다중 소스 가사 스크래핑 함수 (본문 직접 접속 기능 추가) ---
def scrape_multiple_sources(query):
    print(f"  -> 🔍 [진행 중] '{query}' 여러 사이트에서 가사를 수집하고 있습니다...")
    results_text = ""
    lyric_count = 1
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    # 소스 1: Genius 사이트 검색
    try:
        encoded_query = urllib.parse.quote(query + " lyrics")
        search_url = f"https://genius.com/api/search/multi?per_page=1&q={encoded_query}"
        
        response = requests.get(search_url, headers=headers, timeout=5)
        if response.status_code == 200:
            search_data = response.json()
            song_url = None
            for section in search_data.get('response', {}).get('sections', []):
                if section.get('type') == 'song' and section.get('hits'):
                    song_url = section['hits'][0]['result']['url']
                    break
                    
            if song_url:
                page_response = requests.get(song_url, headers=headers, timeout=5)
                page_response.raise_for_status()
                soup = BeautifulSoup(page_response.text, 'html.parser')
                lyrics_divs = soup.find_all('div', class_=lambda x: x and 'Lyrics__Container' in x)
                genius_lyrics = "".join([div.get_text(separator='\n').strip() + "\n\n" for div in lyrics_divs]).strip()
                
                if genius_lyrics:
                    print(f"\n  📝 [가사{lyric_count}] 출처: Genius (원문 가사 전문)")
                    results_text += f"\n[가사{lyric_count}] 출처: Genius\n{genius_lyrics}\n"
                    lyric_count += 1
    except Exception:
        print(f"    - Genius 접속 대기 중 (일시적 차단 또는 에러)")

    # 소스 2: DuckDuckGo 검색 후 -> 사이트 직접 접속해서 긁어오기 (핵심 개선)
    try:
        ddgs = DDGS()
        ddg_results = None
        
        # 1차 시도: 깐깐하게 '가사 발음'까지 검색
        try:
            ddg_results = ddgs.text(f"{query} 가사 발음", max_results=3)
        except Exception:
            pass # 못 찾으면 조용히 넘어감
            
        # 2차 시도: 1차에서 실패했거나 결과가 0개면, '발음'을 빼고 넓게 재검색!
        if not ddg_results:
            print(f"    - ⚠️ '{query} 가사 발음' 검색 실패. '{query} 가사'로 재검색합니다...")
            try:
                ddg_results = ddgs.text(f"{query} 가사", max_results=3)
            except Exception:
                pass
                
        if ddg_results:
            for r in ddg_results:
                title = r.get('title', '제목 없음')
                link = r.get('href', r.get('link', ''))
                
                if link:
                    print(f"    -> 🔗 '{title}' 사이트 본문에 접속 시도 중...")
                    try:
                        # 1. 검색된 블로그나 웹사이트 URL로 직접 접속
                        page_resp = requests.get(link, headers=headers, timeout=5)
                        page_resp.raise_for_status()
                        soup_web = BeautifulSoup(page_resp.text, 'html.parser')
                        
                        # 2. 본문(body) 안에 있는 모든 텍스트를 추출
                        for junk in soup_web(['script', 'style', 'nav', 'footer', 'header']):
                            junk.decompose()
                        if soup_web.body:
                            body_text = (soup_web.find('article') or soup_web.find('main') or soup_web.body).get_text(separator='\n', strip=True)
                            
                            # 3. 사이트 내용이 너무 길면 AI 토큰 초과가 발생하므로 앞부분 5000자만 자름
                            if body_text:
                                body_text = body_text[:5000]
                                print(f"  📝 [가사{lyric_count}] 출처: 웹 스크래핑 성공 ({title})")
                                results_text += f"\n[가사{lyric_count}] 출처: {link}\n{body_text}\n"
                                lyric_count += 1
                    except Exception:
                        print(f"    - 사이트 직접 접속 실패: {link}")
    except Exception as e:
        print(f"    - 웹 검색 실패: {e}")
        
    return results_text.strip()

# --- 4. AI 데이터 생성 및 파싱 함수 ---
def generate_ai_content(song_title, raw_lyrics):

    # AI 분석 호출 로직 부분
    max_retries = 3  # 최대 재시도 횟수
    for attempt in range(1, max_retries + 1):
        error_msg = "" # 에러 메시지 초기화
        for current_model in FALLBACK_MODELS:
            try:
                print(f"  -> 🤖 [진행 중] AI가 수집된 가사를 분석하고 교차 검증하는 중... (사용 모델: {current_model})")

                # prompt.py에서 프롬프트를 불러옴
                prompt_text = get_music_analysis_prompt(song_title, raw_lyrics)
                response = generate_text(prompt_text, current_model)
                return response.text # 성공하면 루프 탈출
            
            except Exception as e:
                error_msg = str(e)
                
                # 429 에러(한도 초과)인지 확인
                if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                    print(f"    - ⚠️ [{current_model}] 한도 초과. 즉시 다음 모델로 전환합니다.")
                    continue # 바로 다음 모델로 넘어감
                else:
                    # 429가 아닌 전혀 다른 에러일 경우 바로 중단
                    print(f"  -> ❌ [오류] AI 생성 중 알 수 없는 오류가 발생했습니다: {error_msg}")
                    continue # 다른 모델로 재시도

        # 모든 모델 실패 시 헬퍼 함수로 딜레이 계산 후 대기
        if attempt < max_retries:
            delay = get_retry_delay(error_msg)
            print(f"  -> ⏳ [API 대기] 모든 모델 분석 실패. {delay}초 후 처음부터 재시도합니다...")
            stop_event.wait(delay)
            if stop_event.is_set():
                return None

    print("  -> ❌ [실패] 재시도 횟수를 초과했습니다. 잠시 후 '2'번(수동 복구 모드)을 이용해 주세요.")
    return None # 또는 상황에 맞게 중단 처리

# --- 파일 감시: 이벤트 콜백은 예약만 하고 단일 작업자가 처리합니다. ---
class ObsidianNoteHandler(FileSystemEventHandler):
    def __init__(self):
        self.guard = threading.Lock()
        self.pending = {}
        self.attempted = {}
        self.locks = {}
        self.worker = threading.Thread(target=self._run, daemon=False)
        self.worker.start()

    def schedule(self, path):
        if path.lower().endswith('.md') and not stop_event.is_set():
            with self.guard:
                self.pending[os.path.normcase(os.path.abspath(path))] = time.monotonic() + 2

    def on_created(self, event):
        if not event.is_directory:
            self.schedule(event.src_path)

    on_modified = on_created

    def on_moved(self, event):
        if not event.is_directory:
            self.schedule(event.dest_path)

    def _run(self):
        while not stop_event.wait(0.25):
            with self.guard:
                paths = [p for p, deadline in self.pending.items() if deadline <= time.monotonic()]
                for path in paths:
                    self.pending.pop(path, None)
            for path in paths:
                if stop_event.is_set():
                    break
                try:
                    content = Path(path).read_text(encoding='utf-8')
                    if ready(content) and path not in self.attempted and not is_playlist_managed(content):
                        self.update_note(path)
                except OSError:
                    pass

    def update_note(self, filepath):
        return self._update(filepath, False)

    def update_lyrics_only(self, filepath):
        return self._update(filepath, True)

    def _update(self, filepath, lyrics_only):
        path = os.path.normcase(os.path.abspath(filepath))
        with self.guard:
            lock = self.locks.setdefault(path, threading.Lock())
        with lock:
            try:
                original = Path(path).read_bytes()
                content = original.decode('utf-8').replace('\r\n', '\n')
                if not ready(content):
                    print('템플릿이 미완성이거나 Music.md 형식이 아닙니다. 저장하지 않습니다.')
                    return False
                # An automatic attempt is made once per path per session; manual repair can retry.
                self.attempted[path] = True
                title = song_title(content, path)
                if not title or stop_event.is_set():
                    return False
                print(f'정보 수집 중: {title}')
                query = get_search_query(title)
                if stop_event.is_set():
                    return False
                raw = scrape_multiple_sources(query or title)
                if not raw or stop_event.is_set():
                    print('가사 자료 수집 실패: 기존 노트를 유지합니다.')
                    return False
                if lyrics_only:
                    result = None
                    for model in FALLBACK_MODELS:
                        try:
                            result = generate_text(get_lyrics_only_prompt(title, raw), model).text
                            if result:
                                break
                        except Exception as exc:
                            print(f'모델 호출 실패: {exc}')
                else:
                    result = generate_ai_content(title, raw)
                if not result or stop_event.is_set():
                    return False
                updated = render_note(content, result, lyrics_only)
                if updated == content:
                    print('채울 수 있는 AI 결과가 없습니다. 원본을 유지합니다.')
                    return False
                if b'\r\n' in original:
                    updated = updated.replace('\n', '\r\n')
                atomic_save(path, original, updated.encode('utf-8'))
                print(f'처리 완료: {title}')
                return True
            except Exception as exc:
                print(f'노트 업데이트 중단: {exc}')
                return False

    def close(self):
        stop_event.set()
        self.worker.join()

# --- 7. Obsidian 실행 여부 확인 ---
OBSIDIAN_CHECK_INTERVAL = 5  # 초

def is_obsidian_running():
    """Obsidian 프로세스가 실행 중인지 확인합니다."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Obsidian.exe", "/NH"],
                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
            ).stdout
            return "obsidian.exe" in out.lower()
        return subprocess.run(["pgrep", "-xi", "obsidian"], capture_output=True).returncode == 0
    except Exception:
        # 확인 자체가 불가능하면 스크립트를 막지 않도록 실행 중으로 간주
        return True

def launch_obsidian(timeout=30):
    """Obsidian을 실행하고, 프로세스가 뜰 때까지 기다립니다. 성공 여부를 반환합니다."""
    try:
        if sys.platform == "win32":
            os.startfile("obsidian://")  # 설치 시 등록되는 URI 프로토콜 사용
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Obsidian"])
        else:
            subprocess.Popen(["obsidian"])
    except Exception as e:
        print(f"❌ Obsidian 실행 실패: {e}")
        return False

    for _ in range(timeout):
        if is_obsidian_running():
            return True
        time.sleep(1)
    return False

def watch_obsidian(observer):
    """Obsidian이 종료되면 감시를 멈추고 스크립트를 종료합니다."""
    while not stop_event.wait(OBSIDIAN_CHECK_INTERVAL):
        if not is_obsidian_running():
            print("\n🛑 Obsidian이 종료되어 스크립트를 함께 종료합니다.")
            observer.stop()
            stop_event.set()
            return

console_lines = queue.Queue()


def is_playlist_managed(content):
    # Selection-generated notes are processed only by explicit selection/manual repair.
    from note_format import split_note
    return split_note(content)[1].get('playlist_managed') is True


def read_console():
    while True:
        line = sys.stdin.readline()
        console_lines.put(line)
        if not line:
            return


def read_input(prompt=""):
    if prompt:
        print(prompt, end="", flush=True)
    while not stop_event.is_set():
        try:
            line = console_lines.get(timeout=0.2)
            if not line:
                raise EOFError
            return line.rstrip('\r\n')
        except queue.Empty:
            pass
    raise EOFError

# --- 8. 메인 실행부 ---
if __name__ == "__main__":
    if not GEMINI_API_KEY or not FALLBACK_MODELS:
        sys.exit(".env의 GEMINI_API_KEY 및 GEMINI_FALLBACK_MODELS를 확인하세요.")
    threading.Thread(target=read_console, daemon=True).start()
    if not is_obsidian_running():
        print("🚀 Obsidian이 꺼져 있어 자동으로 실행합니다...")
        if not launch_obsidian():
            print("❌ Obsidian을 실행하지 못했어요. 직접 켠 뒤 다시 실행해 주세요.")
            sys.exit(1)
        time.sleep(3)  # 볼트 로딩 여유 시간

    target_folder = os.environ.get("OBSIDIAN_FOLDER_PATH")

    if not target_folder:
        print("❌ .env 파일에서 OBSIDIAN_FOLDER_PATH를 찾을 수 없어요.")
        exit()

    if not os.path.exists(target_folder):
        os.makedirs(target_folder)
        
    playlist_service = configured_queue(target_folder, stop_event.is_set)
    handler = ObsidianNoteHandler()
    
    print("\n==================================")
    print(" 🎵 음악 노트 자동화 스크립트 🎵 ")
    print("==================================")
    
    # 1. 스크립트 시작과 동시에 옵저버(1번 감시 모드) 즉시 실행!
    observer = Observer()
    observer.schedule(handler, target_folder, recursive=True)
    observer.start()
    threading.Thread(target=watch_obsidian, args=(observer,), daemon=True).start()

    print(f"🎧 [{target_folder}]\n템플릿 감시를 자동으로 시작했습니다.")
    print("💡 전체 수동 복구(2) | 가사 재수정(3) | 새 곡 확인(4) | 대기 곡 선택·생성(5) | 종료(q)")

    try:
        while True:
            # 백그라운드에서 감시가 돌아가는 동안, 메인 화면은 사용자의 입력을 조용히 기다립니다.
            choice = read_input().strip()
            
            if choice == '2':
                # 올바른 경로를 입력할 때까지 반복해서 묻는 내부 루프
                while True:
                    filepath = read_input("\n[수동 복구 모드] 복구할 파일명(또는 전체 경로)을 입력해 주세요 (취소는 'c'):\n").strip()
                    
                    if filepath.lower() == 'c':
                        print("수동 복구를 취소했습니다. 계속해서 폴더를 감시합니다.\n")
                        break  # 내부 루프를 깨고 메인 대기 화면으로 자연스럽게 복귀
                        
                    filepath = filepath.strip('"').strip("'")
                    
                    # 상대 경로(파일명)만 입력해도 절대 경로로 변환
                    if not os.path.isabs(filepath):
                        filepath = os.path.join(target_folder, filepath)

                    if os.path.exists(filepath) and filepath.endswith('.md'):
                        handler.update_note(filepath)
                        break  # 성공적으로 마쳤으니 내부 루프 탈출
                    else:
                        print("❌ 파일을 찾을 수 없거나 마크다운(.md) 파일이 아닙니다. 경로를 다시 확인해 주세요.")
                        # break가 없으므로 곧바로 input() 창이 다시 떠서 재시도 가능!

            elif choice == '3':
                while True:
                    filepath = read_input("\n[가사 전용 수정] 수정할 파일명(또는 경로)을 입력하세요 (취소: 'c'):\n").strip()
                    if filepath.lower() == 'c':
                        print("가사 수정을 취소했습니다. 감시 모드로 돌아갑니다.\n")
                        break

                    filepath = filepath.strip('"').strip("'")
                    if not os.path.isabs(filepath):
                        filepath = os.path.join(target_folder, filepath)

                    if os.path.exists(filepath) and filepath.endswith('.md'):
                        handler.update_lyrics_only(filepath)
                        break
                    else:
                        print("❌ 파일을 찾을 수 없거나 마크다운(.md) 파일이 아니에요.")

            elif choice in ('4', '5'):
                playlist_menu(playlist_service, choice, read_input, handler.update_note)

            elif choice.lower() == 'q':
                print("\n스크립트를 종료합니다.")
                observer.stop()
                break
                
    # Ctrl+C (KeyboardInterrupt) 또는 Ctrl+Z (EOFError) 입력 시 에러 없이 안전하게 종료
    except (KeyboardInterrupt, EOFError):
        print("\n스크립트를 안전하게 종료합니다.")
        observer.stop()
        
    stop_event.set()
    observer.stop()
    observer.join()
    handler.close()
