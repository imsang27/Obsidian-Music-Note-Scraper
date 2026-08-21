import os
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

from prompt import get_music_analysis_prompt

# .env 파일 불러오기
load_dotenv()

# --- 1. API 설정 ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
client = genai.Client(api_key=GEMINI_API_KEY)

# --- 2. 원곡 정보 추출 함수 ---
def get_search_query(song_title):
    print(f"  -> 🔎 [진행 중] '{song_title}'에서 원곡 정보를 파악하는 중...")
    prompt = f"다음 유튜브 영상 제목에서 '원곡 가수'와 '원곡 제목'을 추출해서 '가수 제목' 형태로만 출력해줘. 커버곡이라도 원곡을 기준으로 해. 부가 설명 없이 딱 검색어만 출력해.\n제목: {song_title}"
    
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        query = response.text.strip()
        print(f"  -> 🎯 [완료] 검색어 확정: {query}")
        return query
    except Exception as e:
        print(f"  -> ❌ 원곡 추출 중 오류 발생: {e}")
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
                if section.get('hits'):
                    song_url = section['hits'][0]['result']['url']
                    break
                    
            if song_url:
                page_response = requests.get(song_url, headers=headers, timeout=5)
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
        ddg_results = ddgs.text(f"{query} 가사 발음", max_results=3)
        if ddg_results:
            for r in ddg_results:
                title = r.get('title', '제목 없음')
                link = r.get('href', r.get('link', ''))
                
                if link:
                    print(f"    -> 🔗 '{title}' 사이트 본문에 접속 시도 중...")
                    try:
                        # 1. 검색된 블로그나 웹사이트 URL로 직접 접속
                        page_resp = requests.get(link, headers=headers, timeout=5)
                        soup_web = BeautifulSoup(page_resp.text, 'html.parser')
                        
                        # 2. 본문(body) 안에 있는 모든 텍스트를 추출
                        if soup_web.body:
                            body_text = soup_web.body.get_text(separator='\n', strip=True)
                            
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
        
    return results_text if results_text.strip() else "수집된 가사 정보가 없습니다."

# --- 4. AI 데이터 생성 및 파싱 함수 (외부 프롬프트 파일 적용) ---
def generate_ai_content(song_title, raw_lyrics):
    # AI 분석 호출 로직 부분
    max_retries = 3  # 최대 재시도 횟수
    retry_delay = 30 # 대기 시간 (초)

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  -> 🤖 [진행 중] AI가 수집된 가사를 분석하고 교차 검증하는 중...")
            
            # prompt.py에서 프롬프트를 불러옵니다
            prompt_text = get_music_analysis_prompt(song_title, raw_lyrics)
    
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt_text)
            return response.text # 성공하면 루프 탈출
        
        except Exception as e:
            error_msg = str(e)
            
            # 429 에러(한도 초과)인지 확인
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                if attempt < max_retries:
                    print(f"  -> ⏳ [API 대기] 제미나이 호출 한도에 도달했습니다. {retry_delay}초 후 재시도합니다...")
                    time.sleep(retry_delay) # 30초 대기 후 다시 for문 처음(try)으로 돌아감
                else:
                    print("  -> ❌ [실패] 재시도 횟수를 초과했습니다. 잠시 후 '2'번(수동 복구 모드)을 이용해 주세요.")
                    return # 또는 상황에 맞게 중단 처리
            else:
                # 429가 아닌 전혀 다른 에러일 경우 바로 중단
                print(f"  -> ❌ [오류] AI 생성 중 알 수 없는 오류가 발생했습니다: {error_msg}")
                return

# --- 5. 속성값 밀림 원천 차단 함수 ---
def extract_property(key, text):
    match = re.search(rf'^{key}::\s*([^\n]*)', text, re.MULTILINE)
    if match:
        val = match.group(1).strip()
        # 만약 AI가 실수로 'album:: lyricist::' 처럼 여러 속성을 한 줄에 출력했다면, 밀림을 방지하기 위해 빈칸 처리
        if "::" in val:
            return ""
        return val
    return ""

def extract_section(tag, text):
    pattern = rf'\[{tag}\]\s*\n?(.*?)(?=\n\[|$)'
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""

# --- 6. 파일 변경 처리 및 업데이트 ---
class ObsidianNoteHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
            
        filepath = event.src_path
        print(f"\n📄 [새 노트 감지됨] {filepath}")
        
        time.sleep(2) 
        self.update_note(filepath)

    def update_note(self, filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                
            # 1. title: "..." 안에 있는 텍스트 전체를 가져옴
            title_match = re.search(r'title:\s*"(.*?)"', content)
            
            if title_match:
                title = title_match.group(1).strip()
                # 2. 맨 앞의 [태그] 부분만 깔끔하게 지움
                # (예: "[with. HONEYZ] 아야" -> "아야")
                title = re.sub(r'^\[.*?\]\s*', '', title)
            else:
                title = None
            
            if not title:
                fallback_match = re.search(r'!\[(.*?)\]\(https://www.youtube.com', content)
                title = fallback_match.group(1).strip() if fallback_match else "알 수 없는 곡"
                
            print(f"\n🚀 [작업 시작] '{title}' 정보 수집 및 업데이트를 진행합니다.")
            
            status_msg = "> ⏳ **AI가 인터넷 검색을 통해 곡 정보와 가사를 수집하는 중입니다... 잠시만 기다려주세요!**\n\n"
            if status_msg not in content:
                if content.startswith("---"):
                    end_idx = content.find("\n---", 3)
                    if end_idx != -1:
                        insert_pos = end_idx + 4
                        content = content[:insert_pos] + "\n\n" + status_msg + content[insert_pos:]
                    else:
                        content = status_msg + content
                else:
                    content = status_msg + content
                    
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)

            search_query = get_search_query(title)
            raw_lyrics = scrape_multiple_sources(search_query)
            # 함수를 호출해서 쓰는 부분
            ai_result = generate_ai_content(title, raw_lyrics)
            
            content = content.replace("\n\n" + status_msg, "").replace(status_msg, "")
            
            # 텅 빈 값(None)이 돌아왔을 때의 안전장치
            if not ai_result:
                print("❌ AI 분석에 실패하여 파일 업데이트를 건너뜁니다.")
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                return  # (루프 안이라면 continue)
            
            # 1. 문자열 속성 채우기
            str_keys = ['vocal', 'group', 'album', 'original_song', 'original_artist', 'description']
            for key in str_keys:
                if re.search(rf'^{key}:\s*(?:(?:\"\")|(?:\'\')|(?:\s*))$', content, re.MULTILINE):
                    val = extract_property(key, ai_result)
                    if val:
                        content = re.sub(rf'^({key}:).*$', rf'\1 "{val}"', content, flags=re.MULTILINE)
                        
            # 2. 리스트 속성 채우기
            list_keys = ['lyricist', 'composer', 'arranger', 'genre', 'mood']
            for key in list_keys:
                if re.search(rf'^{key}:\s*(?:(?:\"\")|(?:\'\')|(?:\s*)|(?:\[\]))$', content, re.MULTILINE):
                    val = extract_property(key, ai_result)
                    if val:
                        items = [i.strip() for i in val.split(',') if i.strip()]
                        if items:
                            list_str = f"{key}:\n" + "\n".join([f"  - {item}" for item in items])
                            content = re.sub(rf'^{key}:\s*(?:(?:\"\")|(?:\'\')|(?:\s*)|(?:\[\]))$', list_str, content, flags=re.MULTILINE)
            
            # 3. 태그(tags) 추가하기
            tags_val = extract_property('tags', ai_result)
            if tags_val:
                tags_list = [re.sub(r'^[-#\s]*', '', t).strip() for t in tags_val.split(',') if t.strip()]
                if tags_list:
                    tags_match = re.search(r'(tags:\n(?:  - .*\n)*)', content)
                    if tags_match:
                        existing_tags = tags_match.group(1)
                        new_tags_str = ""
                        for t in tags_list:
                            if t and f"- {t}" not in existing_tags:
                                new_tags_str += f"  - {t}\n"
                        if new_tags_str:
                            content = content.replace(existing_tags, existing_tags + new_tags_str)

            # 4. 본문 곡 정보 채우기
            story_match = re.search(r'(- \*\*핵심 서사\*\*\n)([\s\S]*?)(?=- \*\*음악적 특징\*\*)', content)
            if story_match and not story_match.group(2).strip():
                story = extract_section('story', ai_result)
                story_lines = [line.lstrip('-* ') for line in story.split('\n') if line.strip()]
                story_indented = "\n".join([f"  {line}" for line in story_lines])
                content = content.replace(story_match.group(0), f"- **핵심 서사**\n{story_indented}\n\n")
                
            features_match = re.search(r'(- \*\*음악적 특징\*\*\n)([\s\S]*?)(?=## 🎤 가사)', content)
            if features_match and not features_match.group(2).strip():
                features = extract_section('features', ai_result)
                features_lines = [line.lstrip('-* ') for line in features.split('\n') if line.strip()]
                features_indented = "\n".join([f"  {line}" for line in features_lines])
                content = content.replace(features_match.group(0), f"- **음악적 특징**\n{features_indented}\n\n")
            
            # 5. 가사 채우기
            lyrics_match = re.search(r'(``` title=".*?"\n)([\s\S]*?)(```)', content)
            if lyrics_match and not lyrics_match.group(2).strip():
                lyrics = extract_section('lyrics', ai_result)
                content = content.replace(lyrics_match.group(0), f"{lyrics_match.group(1)}{lyrics}\n{lyrics_match.group(3)}")
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
                
            print(f"✨ [작업 완료] [{title}] 노트 맞춤형 업데이트 완료! 옵시디언을 확인해 보세요.")
            
        except Exception as e:
            print(f"❌ 노트 업데이트 중 오류 발생: {e}")

# --- 7. 메인 실행부 ---
if __name__ == "__main__":
    target_folder = os.environ.get("OBSIDIAN_FOLDER_PATH") 
    
    if not target_folder:
        print("❌ .env 파일에서 OBSIDIAN_FOLDER_PATH를 찾을 수 없어요.")
        exit()

    if not os.path.exists(target_folder):
        os.makedirs(target_folder)
        
    handler = ObsidianNoteHandler()
    
    print("\n==================================")
    print(" 🎵 음악 노트 자동화 스크립트 🎵 ")
    print("==================================")
    
    # 1. 스크립트 시작과 동시에 옵저버(1번 감시 모드) 즉시 실행!
    observer = Observer()
    observer.schedule(handler, target_folder, recursive=True)
    observer.start()
    
    print(f"🎧 [{target_folder}]\n템플릿 감시를 자동으로 시작했습니다.")
    print("💡 감시 중 특정 파일을 수동 복구하려면 언제든 '2'를 입력하고 엔터를 누르세요. (종료는 'q' 입력)")
    
    try:
        while True:
            # 백그라운드에서 감시가 돌아가는 동안, 메인 화면은 사용자의 입력을 조용히 기다립니다.
            choice = input().strip()
            
            if choice == '2':
                # 올바른 경로를 입력할 때까지 반복해서 묻는 내부 루프
                while True:
                    filepath = input("\n[수동 복구 모드] 복구할 파일명(또는 전체 경로)을 입력해 주세요 (취소는 'c'):\n").strip()
                    
                    if filepath.lower() == 'c':
                        print("수동 복구를 취소했습니다. 계속해서 폴더를 감시합니다.\n")
                        break  # 내부 루프를 깨고 메인 대기 화면으로 자연스럽게 복귀
                        
                    filepath = filepath.strip('"').strip("'")
                    
                    # 상대 경로(파일명)만 입력해도 절대 경로로 변환
                    if not os.path.isabs(filepath):
                        filepath = os.path.join(target_folder, filepath)

                    if os.path.exists(filepath) and filepath.endswith('.md'):
                        handler.update_note(filepath)
                        print("✅ 수동 복구 완료! 다시 폴더 감시 상태로 돌아갑니다.\n")
                        break  # 성공적으로 마쳤으니 내부 루프 탈출
                    else:
                        print("❌ 파일을 찾을 수 없거나 마크다운(.md) 파일이 아닙니다. 경로를 다시 확인해 주세요.")
                        # break가 없으므로 곧바로 input() 창이 다시 떠서 재시도 가능!

            elif choice.lower() == 'q':
                print("\n스크립트를 종료합니다.")
                observer.stop()
                break
                
    except KeyboardInterrupt:
        print("\n스크립트를 종료합니다.")
        observer.stop()
        
    observer.join()