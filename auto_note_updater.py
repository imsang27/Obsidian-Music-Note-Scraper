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

# .env 파일 불러오기
load_dotenv()

# --- 1. API 설정 ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
client = genai.Client(api_key=GEMINI_API_KEY)

# --- 2. 원곡 정보 추출 함수 ---
def get_search_query(song_title):
    print(f"🔎 '{song_title}'에서 원곡 정보를 파악하는 중...")
    prompt = f"다음 유튜브 영상 제목에서 '원곡 가수'와 '원곡 제목'을 추출해서 '가수 제목' 형태로만 출력해줘. 커버곡이라도 원곡을 기준으로 해. 부가 설명 없이 딱 검색어만 출력해.\n제목: {song_title}"
    
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        query = response.text.strip()
        print(f"🎯 검색어 확정: {query}")
        return query
    except Exception as e:
        print(f"❌ 원곡 추출 중 오류 발생: {e}")
        return song_title

# --- 3. 가사 스크래핑 함수 ---
def scrape_lyrics(query):
    print(f"🔍 '{query}' 가사 검색 중...")
    # 가사 검색 정확도를 높이기 위해 검색어 뒤에 'lyrics'를 붙여요
    encoded_query = urllib.parse.quote(query + " lyrics")
    search_url = f"https://genius.com/api/search/multi?per_page=1&q={encoded_query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        response = requests.get(search_url, headers=headers)
        search_data = response.json()
        
        song_url = None
        for section in search_data.get('response', {}).get('sections', []):
            if section.get('hits'):
                song_url = section['hits'][0]['result']['url']
                break
                
        if not song_url:
            return "가사를 찾지 못했어요."
            
        page_response = requests.get(song_url, headers=headers)
        soup = BeautifulSoup(page_response.text, 'html.parser')
        
        lyrics_divs = soup.find_all('div', class_=lambda x: x and 'Lyrics__Container' in x)
        lyrics_text = "".join([div.get_text(separator='\n').strip() + "\n\n" for div in lyrics_divs])
        
        return lyrics_text.strip() if lyrics_text.strip() else "가사 텍스트를 추출하지 못했어요."
            
    except Exception as e:
        return f"크롤링 오류 발생: {e}"

# --- 4. AI 데이터 생성 및 파싱 함수 ---
def generate_ai_content(song_title, raw_lyrics):
    print(f"🤖 '{song_title}' AI 분석 및 데이터 추출 중...")
    
    prompt = f"""
당신은 깊이 있는 음악 평론가이자 전문 번역가입니다.
제공된 곡 제목과 원문 가사를 바탕으로 아래 양식을 복사하여 빈칸을 채워주세요.

[입력 데이터]
- 곡 제목: {song_title}
- 1차 수집된 원문 가사 (오류가 있을 수 있음):
{raw_lyrics}

[요청 사항]
1. 속성(Properties): 값이 없다면 절대 지우지 말고 `키:: ` 형태로 비워두세요.
   - lyricist, composer: 사람 이름은 `원어명 (영문명)` 형태로 적으세요. 단, 원어명이 이미 영어라면 괄호 없이 한 번만 적으세요 (예: Heavenz). 여러 명이면 쉼표(,)로 구분하세요.
   - genre: 반드시 '영어'로 작성하세요. 여러 개면 쉼표(,)로 구분하세요.
   - mood: '신남', '몽환', '슬픔'처럼 반드시 명사형 단어로 적고, 여러 개면 쉼표(,)로 구분하세요.
   - description: 이 곡에 대한 1~2문장의 짧고 간결한 곡 설명을 적으세요. (타이업 정보 제외)
   - tags: 타이업(애니/영화/게임 등) 매체명은 이곳에 적어주세요. 영어가 아니라면 한국어로 번역하고, 띄어쓰기는 언더바(_)로 대체하세요. genre나 mood와 중복되는 단어는 제외하고 쉼표(,)로 구분하세요.
2. 텍스트 작성: 핵심 서사와 음악적 특징은 마크다운 기호(- 등)를 절대 쓰지 말고, 3~4개의 문장을 각각 줄바꿈(엔터)으로만 구분해서 작성하세요.
3. 가사 통합 및 정확도 향상: 
   - 제공된 원문 가사가 곡과 일치하지 않거나 오류(crumbs 등)가 있다면 과감히 버리세요.
   - 당신의 방대한 음악 지식과 인터넷에 흩어진 여러 사이트의 가사 데이터를 종합하여 가장 완벽하고 정확한 가사로 대체하세요.
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
        print(f"❌ AI 생성 중 오류 발생: {e}")
        return ""

def extract_property(key, text):
    # [핵심 수정] 줄바꿈(\n) 전까지만 딱 가져와서 속성이 밀리는 버그를 완전히 차단해요!
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
        print(f"\n📄 새로운 노트 감지됨: {filepath}")
        
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
            
            search_query = get_search_query(title)
            raw_lyrics = scrape_lyrics(search_query)
            ai_result = generate_ai_content(title, raw_lyrics)
            
            if not ai_result:
                return
            
            # 1. 문자열 속성 채우기 (인라인 형태)
            str_keys = ['vocal', 'group', 'album', 'original_song', 'original_artist', 'description']
            for key in str_keys:
                if re.search(rf'^{key}:\s*(?:(?:\"\")|(?:\'\')|(?:\s*))$', content, re.MULTILINE):
                    val = extract_property(key, ai_result)
                    if val:
                        content = re.sub(rf'^({key}:).*$', rf'\1 "{val}"', content, flags=re.MULTILINE)
                        
            # 2. 리스트 속성 채우기 (아래로 나열되는 멀티라인 리스트 형태 적용)
            list_keys = ['lyricist', 'composer', 'genre', 'mood']
            for key in list_keys:
                if re.search(rf'^{key}:\s*(?:(?:\"\")|(?:\'\')|(?:\s*)|(?:\[\]))$', content, re.MULTILINE):
                    val = extract_property(key, ai_result)
                    if val:
                        items = [i.strip() for i in val.split(',') if i.strip()]
                        if items:
                            list_str = f"{key}:\n" + "\n".join([f"  - {item}" for item in items])
                            content = re.sub(rf'^{key}:\s*(?:(?:\"\")|(?:\'\')|(?:\s*)|(?:\[\]))$', list_str, content, flags=re.MULTILINE)
            
            # 3. 태그(tags) 추가하기 (중복 방지)
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

            # 4. 본문 곡 정보 채우기 (불릿 기호 없이 들여쓰기만 적용)
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
                
            print(f"✨ [{title}] 노트 맞춤형 업데이트 완료!")
            
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
                print(f"\n🚀 수동 복구를 시작합니다: {filepath}")
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