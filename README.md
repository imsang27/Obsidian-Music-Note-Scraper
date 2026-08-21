# 🎵 Obsidian Music Note Scraper

Google Gemini API와 다중 웹 검색을 활용하여, 옵시디언(Obsidian) 음악 노트의 메타데이터와 다국어 가사를 자동으로 완성해 주는 파이썬 기반 자동화 스크립트입니다.

> 💡 **참고:** 이 스크립트는 [옵시디언](https://obsidian.md/) 플러그인 **[youtube-template](https://github.com/sundevista/youtube-template)**과 연동하여 사용되도록 설계하였습니다. 해당 플러그인의 설치 및 자세한 사용법은 링크된 깃허브 페이지를 참고해 주세요.

## ✨ 주요 기능
* **실시간 템플릿 감지**: `watchdog`을 활용하여 넓은 범주의 옵시디언 음악 폴더(하위 폴더 포함) 내 마크다운 파일 생성을 실시간으로 감지합니다.
* **다중 소스 가사 수집**: Genius API와 DuckDuckGo 웹 검색을 교차 활용하여 가사 검색 정확도를 극대화합니다.
* **강력한 텍스트 전처리**: 파일명이나 템플릿에 마크다운 URL 링크나 대괄호(`[...]`) 태그가 섞여 있어도 완벽하게 제거하여 정확한 곡 제목만 추출합니다.
* **AI 기반 자동 완성**: Gemini API가 수집된 가사를 분석하여 곡의 메타데이터, 핵심 서사, 음악적 특징을 옵시디언 Frontmatter 및 본문 양식에 맞춰 완벽하게 덮어씁니다.
* **스마트 태그 생성**: 검색 편의성을 위해 타이업 매체 형식(매체/작품명)을 준수하고, 비영어권 아티스트 이름을 영문 로마자로 자동 변환하여 태그를 생성합니다.
* **다국어 가사 통합**: 원문/독음/해석을 깔끔하게 교차 정렬합니다.
* **스마트 Fallback 및 예외 처리 (강조)**: 429 한도 초과 에러 발생 시 지정된 예비 모델로 멈춤 없이 즉시 전환(Cascading)하며, 필요시 에러 메시지를 분석해 스마트하게 대기한 후 스크래핑을 완료합니다.
* **스마트 복구 모드**: 빈칸만 똑똑하게 찾아 채워 넣는 수동 복구 모드를 지원합니다.

## 🚀 설치 및 설정 방법

1. **저장소 클론 및 패키지 설치**
    ```bash
    git clone https://github.com/imsang27/Obsidian-Music-Note-Scraper.git
    cd Obsidian-Music-Note-Scraper
    pip install -r requirements.txt
    ```

2. **환경 변수 설정**
프로젝트 폴더 내에 `.env` 파일을 생성하고 아래 양식에 맞게 작성합니다.  
([.env.semple](./.env.example) 파일을 참고하세요.)

    💡 사용 가능한 최신 API 모델 및 무료 한도 확인: [https://aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit)
    ```env
    # Gemini API 키 (Google AI Studio 발급)
    GEMINI_API_KEY="your_api_key_here"

    # 옵시디언 음악 폴더 경로 (예시)
    OBSIDIAN_FOLDER_PATH="C:\Users\username\Documents\Obsidian\Music"

    # 사용할 AI 모델을 순위별로 쉼표(,)로 구분하여 작성 (Fallback 적용)
    GEMINI_FALLBACK_MODELS="gemini-3.5-flash-lite, gemini-3.1-flash-lite"
    ```

## 💻 사용 방법

스크립트를 실행하면 터미널에서 두 가지 모드 중 하나를 선택할 수 있습니다.

```bash
python auto_music_note.py
```

1. **폴더 자동 감시 모드 (기본)**: 옵시디언에서 표준 YouTube URL(예: `https://www.youtube.com/watch?v=...`)이 포함된 새 노트를 생성하면 백그라운드에서 자동으로 정보를 채웁니다. 실시간으로 옵시디언 화면에 진행 상태가 표시됩니다.
2. **특정 파일 수동 복구 모드**: 스크랩 중 오류가 발생했거나 기존에 작성된 노트의 경로를 직접 입력하면, 빈 속성만 식별하여 안전하게 정보를 업데이트합니다.

## 📄 라이선스

이 프로젝트는 [Apache License 2.0](http://www.apache.org/licenses/)을 따릅니다.
