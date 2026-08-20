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

# --- 4. AI 데이터 생성 및 파싱 함수 (강력한 통제 프롬프트 적용) ---
def generate_ai_content(song_title, raw_lyrics):
    print(f"  -> 🤖 [진행 중] AI가 수집된 가사를 분석하고 교차 검증하는 중...")
    
    prompt = f"""
당신은 깊이 있는 음악 평론가이자 전문 번역가입니다.
제공된 곡 제목과 여러 소스에서 수집된 가사 데이터를 바탕으로 아래 양식을 복사하여 빈칸을 채워주세요.

[입력 데이터]
- 곡 제목: {song_title}
- 다중 수집된 가사 참고 자료:
{raw_lyrics}

[요청 사항]
1. 속성(Properties): 값이 없다면 절대 지우지 말고 `키:: ` 형태로 비워두세요. **(주의: 절대 한 줄에 여러 키를 붙여 쓰지 말고, 반드시 각각 줄바꿈 하세요.)**
   - lyricist, composer: 사람 이름은 `원어명 (영문명)` 형태로 적으세요. 원어명이 이미 영어라면 괄호 없이 한 번만 적으세요 (예: Heavenz). 여러 명이면 쉼표(,)로 구분하세요.
   - genre: 반드시 '영어'로 작성하세요. 여러 개면 쉼표(,)로 구분하세요.
   - mood: '신남', '몽환', '슬픔'처럼 반드시 명사형 단어로 적고, 여러 개면 쉼표(,)로 구분하세요.
   - description: 이 곡에 대한 1~2문장의 짧고 간결한 곡 설명을 적으세요.
   - tags: 타이업 매체명 등을 적되, 띄어쓰기는 언더바(_)로 대체하세요. 절대 하이픈(-)이나 특수기호를 넣지 마세요.
2. 텍스트 작성: 핵심 서사와 음악적 특징은 마크다운 기호(- 등)를 절대 쓰지 말고, 3~4개의 문장을 각각 줄바꿈(엔터)으로만 구분해서 작성하세요.
3. 가사 통합 및 정확도 향상 (매우 중요): 
   - 현재 제공된 [다중 수집된 가사 참고 자료]는 웹 검색의 짧은 '미리보기 조각(Snippet)'일 가능성이 높습니다.
   - 제공된 자료가 짧거나 엉뚱한 곡이라면 **완전히 무시**하세요. 
   - 반드시 당신의 내장 데이터베이스에 있는 '{song_title}'의 **실제 원곡 전체 가사**를 끝까지 정확하게 꺼내서 작성하세요. 절대 다른 노래를 섞거나 창작하지 마세요.
   - 일본어 가사인 경우: 원문 한 줄, 독음 한 줄, 해석 한 줄을 교차하세요.
   - **독음(발음)에는 절대 괄호()를 사용하지 마세요.**
   
   (O) 올바른 가사 작성 예시 1:
   本当の事
   혼토우노 코토
   진실

   (X) 틀린 가사 작성 예시 1 (독음에 괄호 절대 금지):
   本当の事
   (혼토우노 코토)
   진실

   (O) 올바른 가사 작성 예시 2:
   過ぎていく現在に抱きしめられている
   스기테유쿠 이마니 다키시메라레테이루
   지나가는 현재에 안겨져있어
   
   (X) 틀린 가사 작성 예시 2 (첫 줄 원문에 요미가나 괄호 포함 절대 금지):
   過ぎていく現在(いま)に抱きしめられている
   스기테유쿠 이마니 다키시메라레테이루
   지나가는 현재에 안겨져있어

   (O) 올바른 가사 작성 예시 3:
   今日を噛み締めよう
   쿄오오 카미시메요오
   오늘을 곱씹자
   
   (X) 틀린 가사 작성 예시 3 (원문과 발음 불일치, 뜬금없는 영어/오역 찌꺼기 혼용 절대 금지):
   今日を bite しまおう
   쿄오오 카미시메테이요오
   오늘을 곱씹자

[출력 양식]
vocal:: 
group:: 
album:: 
lyricist:: 
composer:: 
original_song:: 
original_artist:: 
genre:: 
mood:: 
description:: 
tags:: 

[story]
(여기에 핵심 서사 작성, 불릿 기호 없이 줄바꿈으로만 구분)

[features]
(여기에 음악적 특징 작성, 불릿 기호 없이 줄바꿈으로만 구분)

[lyrics]
(여기에 통합 가사 작성)
"""
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return response.text
    except Exception as e:
        print(f"  -> ❌ AI 생성 중 오류 발생: {e}")
        return ""

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
                
            title_match = re.search(r'title:\s*".*?\[(.*?)\].*?"', content)
            title = title_match.group(1).strip() if title_match else None
            
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
            ai_result = generate_ai_content(title, raw_lyrics)
            
            content = content.replace("\n\n" + status_msg, "").replace(status_msg, "")
            
            if not ai_result:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                return
            
            # 1. 문자열 속성 채우기
            str_keys = ['vocal', 'group', 'album', 'original_song', 'original_artist', 'description']
            for key in str_keys:
                if re.search(rf'^{key}:\s*(?:(?:\"\")|(?:\'\')|(?:\s*))$', content, re.MULTILINE):
                    val = extract_property(key, ai_result)
                    if val:
                        content = re.sub(rf'^({key}:).*$', rf'\1 "{val}"', content, flags=re.MULTILINE)
                        
            # 2. 리스트 속성 채우기
            list_keys = ['lyricist', 'composer', 'genre', 'mood']
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
    print("1. 폴더 자동 감시 모드 (기본)")
    print("2. 특정 파일 수동 복구 모드 (빈칸만 채우기)")
    
    choice = input("\n원하시는 모드의 번호를 입력하세요 (1 또는 2): ").strip()
    
    if choice == '2':
        while True:
            filepath = input("\n복구할 파일의 전체 경로를 붙여넣어 주세요 (취소하려면 'q' 입력):\n").strip()
            
            if filepath.lower() == 'q':
                print("수동 복구를 취소하고 프로그램을 종료합니다.")
                break
                
            filepath = filepath.strip('"').strip("'")
            
            if os.path.exists(filepath) and filepath.endswith('.md'):
                handler.update_note(filepath)
                break
            else:
                print("❌ 파일을 찾을 수 없거나 마크다운(.md) 파일이 아닙니다. 경로를 다시 확인해 주세요.")
    else:
        observer = Observer()
        observer.schedule(handler, target_folder, recursive=True)
        observer.start()
        
        print(f"\n🎧 [{target_folder}] 템플릿 감시를 시작했어요. (종료하려면 Ctrl+C)")
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n감시를 종료할게요.")
            observer.stop()
        observer.join()