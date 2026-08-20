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
from duckduckgo_search import DDGS  # 🌐 다중 웹 검색을 위해 추가된 라이브러리

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

# --- 3. 다중 소스 가사 스크래핑 함수 (피드백 출력 강화) ---
def scrape_multiple_sources(query):
    print(f"  -> 🔍 [진행 중] '{query}' 여러 사이트에서 가사를 수집하고 있습니다...")
    results_text = ""
    lyric_count = 1
    
    # 소스 1: Genius 사이트 검색
    try:
        encoded_query = urllib.parse.quote(query + " lyrics")
        search_url = f"https://genius.com/api/search/multi?per_page=1&q={encoded_query}"
        headers = {"User-Agent": "Mozilla/5.0"}
        
        response = requests.get(search_url, headers=headers)
        search_data = response.json()
        
        song_url = None
        for section in search_data.get('response', {}).get('sections', []):
            if section.get('hits'):
                song_url = section['hits'][0]['result']['url']
                break
                
        if song_url:
            page_response = requests.get(song_url, headers=headers)
            soup = BeautifulSoup(page_response.text, 'html.parser')
            lyrics_divs = soup.find_all('div', class_=lambda x: x and 'Lyrics__Container' in x)
            genius_lyrics = "".join([div.get_text(separator='\n').strip() + "\n\n" for div in lyrics_divs]).strip()
            
            if genius_lyrics:
                # 터미널 실시간 피드백
                print(f"\n  📝 [가사{lyric_count}] 출처: Genius (원문 가사 전문)")
                
                # AI에게 전달할 텍스트에 출처 표기
                results_text += f"\n[가사{lyric_count}] 출처: Genius\n{genius_lyrics}\n"
                lyric_count += 1
    except Exception as e:
        print(f"    - Genius 검색 실패: {e}")

    # 소스 2: DuckDuckGo 웹 전체 검색 (블로그, 커뮤니티 등)
    try:
        ddgs = DDGS()
        ddg_results = ddgs.text(f"{query} 가사 번역", max_results=3)
        if ddg_results:
            for r in ddg_results:
                title = r.get('title', '제목 없음')
                body = r.get('body', '내용 없음')
                
                # 터미널 실시간 피드백 (내용이 길 수 있어 미리보기만 출력해요)
                print(f"\n  📝 [가사{lyric_count}] 출처: 웹 검색 ({title})")
                print(f"  - 내용 미리보기: {body[:60]}...")
                
                # AI에게 전달할 텍스트에 출처 표기
                results_text += f"\n[가사{lyric_count}] 출처: 웹 검색 ({title})\n{body}\n"
                lyric_count += 1
    except Exception as e:
        print(f"    - 웹 검색 실패: {e}")
        
    return results_text if results_text.strip() else "수집된 가사 정보가 없습니다."

# --- 4. AI 데이터 생성 및 파싱 함수 ---
def generate_ai_content(song_title, raw_lyrics):
    print(f"  -> 🤖 [진행 중] AI가 수집된 가사를 분석하고 교차 검증하는 중...")
    
    prompt = f"""
당신은 깊이 있는 음악 평론가이자 전문 번역가입니다.
제공된 곡 제목과 여러 소스에서 수집된 가사 데이터를 바탕으로 아래 양식을 복사하여 빈칸을 채워주세요.

[입력 데이터]
- 곡 제목: {song_title}
- 다중 수집된 가사 참고 자료 (오류가 섞여 있을 수 있음):
{raw_lyrics}

[요청 사항]
1. 속성(Properties): 값이 없다면 절대 지우지 말고 `키:: ` 형태로 비워두세요.
   - lyricist, composer: 사람 이름은 `원어명 (영문명)` 형태로 적으세요. 단, 원어명이 이미 영어라면 괄호 없이 한 번만 적으세요 (예: Heavenz). 여러 명이면 쉼표(,)로 구분하세요.
   - genre: 반드시 '영어'로 작성하세요. 여러 개면 쉼표(,)로 구분하세요.
   - mood: '신남', '몽환', '슬픔'처럼 반드시 명사형 단어로 적고, 여러 개면 쉼표(,)로 구분하세요.
   - description: 이 곡에 대한 1~2문장의 짧고 간결한 곡 설명을 적으세요. (타이업 정보 제외)
   - tags: 타이업(애니/영화/게임 등) 매체명은 이곳에 적어주세요. 영어가 아니라면 한국어로 번역하고, 띄어쓰기는 언더바(_)로 대체하세요. genre나 mood와 중복되는 단어는 제외하고 쉼표(,)로 구분하세요.
2. 텍스트 작성: 핵심 서사와 음악적 특징은 마크다운 기호(- 등)를 절대 쓰지 말고, 3~4개의 문장을 각각 줄바꿈(엔터)으로만 구분해서 작성하세요.
3. 가사 통합 및 정확도 향상 (매우 중요): 
   - 제공된 여러 소스의 가사(Genius, 웹 참고 자료)와 당신의 내부 음악 데이터베이스를 모두 교차 검증하세요.
   - 웹 스크래핑 찌꺼기(crumbs, 코러스 등)나 곡과 일치하지 않는 가사는 완벽하게 걸러내고 가장 정확한 가사만 번역하세요.
   - 일본어 가사인 경우: 원문 한 줄, 독음 한 줄(괄호 없이), 해석 한 줄을 교차하세요.

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

def extract_property(key, text):
    match = re.search(rf'^{key}::\s*([^\n]*)', text, re.MULTILINE)
    return match.group(1).strip() if match else ""

def extract_section(tag, text):
    pattern = rf'\[{tag}\]\s*\n?(.*?)(?=\n\[|$)'
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""

# --- 5. 파일 변경 처리 및 업데이트 ---
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
            
            # --- 실시간 피드백: 옵시디언 파일에 진행 상태 남기기 ---
            status_msg = "⏳ **AI가 인터넷 검색을 통해 곡 정보와 가사를 수집하는 중입니다... 잠시만 기다려주세요!**\n\n"
            if status_msg not in content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(status_msg + content)

            search_query = get_search_query(title)
            raw_lyrics = scrape_multiple_sources(search_query)
            ai_result = generate_ai_content(title, raw_lyrics)
            
            # 상태 메시지 삭제
            content = content.replace(status_msg, "")
            
            if not ai_result:
                # 실패하더라도 파일은 원래 상태로 되돌려 둡니다.
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
                tags_list = [t.strip() for t in tags_val.split(',') if t.strip()]
                if tags_list:
                    tags_match = re.search(r'(tags:\n(?:  - .*\n)*)', content)
                    if tags_match:
                        existing_tags = tags_match.group(1)
                        new_tags_str = ""
                        for t in tags_list:
                            if f"- {t}" not in existing_tags:
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

# --- 6. 메인 실행부 ---
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