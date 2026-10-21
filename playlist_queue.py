"""On-demand playlist discovery and explicitly selected note creation.

State is local, keyed by playlist ID, and never inferred from historical exports.
"""
import copy
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yaml
from dotenv import dotenv_values

from note_format import ready, split_note

DEFAULT_PLAYLIST = 'https://www.youtube.com/playlist?list=PLGvD-qQNEzS8AqkIceGZp_R63WTwraNXD'
KST = timezone(timedelta(hours=9))
VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')


def playlist_id(url):
    value = parse_qs(urlparse(url).query).get('list', [''])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('올바른 플레이리스트 URL이 필요합니다.')
    return value


def video_id(url):
    parsed = urlparse(str(url))
    if parsed.hostname in ('youtu.be', 'www.youtu.be'):
        value = parsed.path.strip('/')
    elif parsed.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        value = parse_qs(parsed.query).get('v', [''])[0]
    else:
        return None
    return value if VIDEO_ID.fullmatch(value) else None


class YouTubeAPI:
    def __init__(self, key, cancelled=lambda: False, get=requests.get):
        self.key, self.cancelled, self.get = key, cancelled, get

    def request(self, resource, **params):
        if self.cancelled():
            raise RuntimeError('작업이 취소되었습니다.')
        if not self.key:
            raise ValueError('YOUTUBE_API_KEY 또는 YOUTUBE_ENV_PATH의 API_KEY를 설정하세요.')
        try:
            response = self.get('https://www.googleapis.com/youtube/v3/' + resource,
                                params={**params, 'key': self.key}, timeout=30)
        except requests.RequestException:
            # requests exceptions can contain the API key in their URL.
            raise RuntimeError('YouTube 연결 실패. 네트워크를 확인하고 다시 시도하세요.') from None
        if response.status_code != 200:
            raise RuntimeError(f'YouTube API 오류 HTTP {response.status_code}. 키·할당량·공개 여부를 확인하세요.')
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError('YouTube 응답이 올바른 JSON이 아닙니다.') from None
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise RuntimeError('YouTube 응답에서 항목 목록을 찾지 못했습니다.')
        return data

    def playlist(self, identifier):
        videos, token, tokens = {}, None, set()
        while True:
            params = dict(part='snippet,contentDetails', playlistId=identifier, maxResults=50)
            if token:
                params['pageToken'] = token
            data = self.request('playlistItems', **params)
            for item in data['items']:
                snippet = item.get('snippet', {})
                vid = snippet.get('resourceId', {}).get('videoId')
                if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid):
                    raise RuntimeError('영상 ID가 없는 응답입니다. 기준 목록을 변경하지 않습니다.')
                video = {'id': vid, 'title': snippet.get('title', ''),
                         'channel': snippet.get('videoOwnerChannelTitle', ''),
                         'added_at': snippet.get('publishedAt', '')}
                if vid not in videos or video['added_at'] > videos[vid]['added_at']:
                    videos[vid] = video
            token = data.get('nextPageToken')
            if not token:
                return videos
            if token in tokens:
                raise RuntimeError('페이지 토큰이 반복됩니다. 저장하지 않습니다.')
            tokens.add(token)

    def details(self, vid):
        items = self.request('videos', part='snippet,contentDetails', id=vid)['items']
        if not items or items[0].get('id') != vid:
            raise ValueError('삭제·비공개 등으로 영상 정보를 가져올 수 없습니다.')
        snippet = items[0]['snippet']
        thumbs = snippet.get('thumbnails', {})
        thumbnail = next((thumbs[k]['url'] for k in ('maxres', 'standard', 'high', 'medium', 'default')
                          if k in thumbs), '')
        return {'id': vid, 'title': snippet['title'], 'channel': snippet['channelTitle'],
                'channel_id': snippet['channelId'], 'published_at': snippet['publishedAt'],
                'thumbnail': thumbnail, 'duration': items[0].get('contentDetails', {}).get('duration', '')}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(path):
    """OS lock releases even after a crash; competing instances fail without writing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('다른 실행 창에서 플레이리스트 작업 중입니다.') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def duration_text(value):
    match = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', value)
    if not match:
        return value
    hours, minutes, seconds = (int(v or 0) for v in match.groups())
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


def render_template(template, video, identifier):
    """Parse placeholder YAML before inserting external strings, so quotes are safe."""
    now = datetime.now(KST).strftime('%Y-%m-%d %a %H:%M:%S')
    template = re.sub(r"<%\s*tp\.file\.(?:creation_date|last_modified_date)\('[^']*'\)\s*%>", now, template)
    if '<%' in template:
        raise ValueError('지원하지 않는 Templater 식이 있습니다. 생성용 템플릿을 확인하세요.')
    match, data = split_note(template)
    url = 'https://www.youtube.com/watch?v=' + video['id']
    title = ' '.join(video['title'].splitlines())
    values = {'title': title, 'channelName': video['channel'], 'channelId': video['channel_id'],
              'youtubeUrl': url, 'id': video['id'], 'thumbnail': video['thumbnail'],
              'length': duration_text(video['duration']), 'publishDate': video['published_at'][:10]}

    def replace(text):
        def value(m):
            if m[1] not in values:
                raise ValueError(f'지원하지 않는 템플릿 값: {m[1]}')
            return values[m[1]]
        return re.sub(r'\{\{(\w+)\}\}', value, text)

    def transform(value):
        if isinstance(value, str):
            return replace(value)
        if isinstance(value, list):
            return [transform(v) for v in value]
        if isinstance(value, dict):
            return {k: transform(v) for k, v in value.items()}
        return value

    data = transform(data)
    data['playlist_managed'] = True  # Watchdog must never retry these without user selection.
    data['youtube_id'] = video['id']
    data['playlist_url'] = 'https://www.youtube.com/playlist?list=' + identifier
    if isinstance(data.get('tags'), list):
        data['tags'] = [re.sub(r'[^\w/\-]', '_', str(t)) for t in data['tags']]
    body = match[4].replace('![[{{thumbnail}}]]', '![썸네일]({{thumbnail}})' if video['thumbnail'] else '')
    # Quotes and backticks in a video title must not break the lyrics code fence.
    body = body.replace('``` title="{{title}}"', '``` title="' + title.replace('"', "'").replace('`', '') + '"')
    content = '---\n' + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10000) + '---\n' + replace(body)
    if not ready(content):
        raise ValueError('생성된 노트가 Music.md 구조와 맞지 않습니다.')
    return content


def existing_notes(folder):
    found = {}
    for path in folder.rglob('*.md'):
        try:
            _, data = split_note(path.read_text(encoding='utf-8-sig'))
        except (ValueError, yaml.YAMLError, UnicodeError):
            continue
        vid = video_id(data.get('Reference', ''))
        if vid:
            found[vid] = path
    return found


class PlaylistQueue:
    def __init__(self, api, url, state_dir, folder, template_path):
        self.api, self.identifier = api, playlist_id(url)
        self.path = Path(state_dir) / (self.identifier + '.json')
        self.folder, self.template_path = Path(folder), Path(template_path)

    def load(self):
        if not self.path.exists():
            return {'version': 1, 'playlist_id': self.identifier, 'initialized': False, 'seen': [], 'entries': {}}
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if (state['version'] != 1 or state['playlist_id'] != self.identifier
                    or not isinstance(state['seen'], list) or not isinstance(state['entries'], dict)
                    or not isinstance(state['initialized'], bool)):
                raise ValueError
            if not all(isinstance(v, str) and VIDEO_ID.fullmatch(v) for v in state['seen']):
                raise ValueError
            for vid, entry in state['entries'].items():
                if (vid not in state['seen'] or entry['id'] != vid
                        or entry['status'] not in ('pending', 'failed', 'completed', 'existing')
                        or not all(isinstance(entry[k], str) for k in ('title', 'channel', 'added_at'))
                        or not (entry.get('note_path') is None or isinstance(entry['note_path'], str))):
                    raise ValueError
            return state
        except (ValueError, KeyError, TypeError):
            raise ValueError('대기 목록 파일이 손상되었습니다. 원본을 보존하고 백업을 확인하세요.') from None

    def check(self):
        with state_lock(self.path):
            state = self.load()
            current = self.api.playlist(self.identifier)  # All pages must succeed before committing.
            baseline = not state['initialized']
            new = set(current) - set(state['seen'])
            if not baseline:
                for vid in new:
                    state['entries'][vid] = {**current[vid], 'status': 'pending', 'note_path': None}
                for vid, entry in state['entries'].items():
                    entry['in_playlist'] = vid in current
                    if vid in current:
                        entry.update(current[vid])
            state['seen'] = sorted(set(state['seen']) | set(current))
            state['initialized'] = True
            state['checked_at'] = datetime.now(KST).isoformat()
            save_state(self.path, state)
            return {'baseline': baseline, 'total': len(current), 'new': 0 if baseline else len(new)}

    def pending(self, keyword='', channel='', limit=None):
        state = self.load()
        entries = [copy.deepcopy(e) for e in state['entries'].values()
                   if e['status'] in ('pending', 'failed')
                   and keyword.casefold() in e['title'].casefold()
                   and channel.casefold() in e['channel'].casefold()]
        entries.sort(key=lambda e: (e['added_at'], e['id']), reverse=True)
        return entries if limit is None else entries[:limit]

    def create(self, vid, enrich):
        with state_lock(self.path):
            state = self.load()
            entry = state['entries'].get(vid)
            if not entry or entry['status'] not in ('pending', 'failed'):
                raise ValueError('생성 가능한 대기 항목이 아닙니다.')
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                found = existing_notes(self.folder).get(vid)
                # A failed, explicitly selected note can be retried; other existing notes stay untouched.
                owned = entry.get('note_path')
                if found and (not owned or found.resolve() != Path(owned).resolve()):
                    entry.update(status='existing', note_path=str(found))
                    save_state(self.path, state)
                    return 'existing'
                if found:
                    path = found
                else:
                    video = self.api.details(vid)
                    content = render_template(self.template_path.read_text(encoding='utf-8-sig'), video, self.identifier)
                    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', video['title']).strip(' .')[:100] or 'Music'
                    path = self.folder / f'{name} [{vid}].md'
                    # Persist ownership first, so a crash after note creation can be resumed explicitly.
                    entry['note_path'] = str(path.resolve())
                    entry['status'] = 'failed'
                    save_state(self.path, state)
                    with path.open('x', encoding='utf-8') as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                if self.api.cancelled():
                    raise RuntimeError('작업이 취소되었습니다. 노트는 보존됩니다.')
                entry['status'] = 'completed' if enrich(str(path)) else 'failed'
                entry['error'] = None if entry['status'] == 'completed' else 'AI 보완 실패. 선택해서 다시 시도하세요.'
            except Exception as exc:
                entry['status'] = 'failed'
                # Do not persist exception text that might contain credentials or request URLs.
                entry['error'] = f'{type(exc).__name__}: 생성 실패. 설정·파일·API 연결을 확인하세요.'
            save_state(self.path, state)
            return entry['status']


def configured_queue(folder, cancelled=lambda: False):
    root = Path(__file__).resolve().parent
    env_path = Path(os.getenv('YOUTUBE_ENV_PATH') or root.parent / 'Youtube-Playlist-API' / '.env')
    extra = dotenv_values(env_path) if env_path.is_file() else {}
    key = os.getenv('YOUTUBE_API_KEY') or os.getenv('API_KEY') or extra.get('API_KEY')
    url = os.getenv('YOUTUBE_PLAYLIST_URL') or DEFAULT_PLAYLIST
    template = os.getenv('MUSIC_TEMPLATE_PATH') or root / 'templates' / 'Music.md'
    return PlaylistQueue(YouTubeAPI(key, cancelled), url, root / '.playlist_state', folder, template)


def choose_ids(text, entries):
    """Only explicit numbers/ranges/URLs can authorize creation; blank cancels."""
    if not text.strip() or text.casefold() == 'c':
        return []
    selected = []
    for token in text.replace(',', ' ').split():
        vid = video_id(token)
        if vid:
            if vid not in {e['id'] for e in entries}:
                raise ValueError('현재 표시된 대기 목록의 URL만 선택할 수 있습니다.')
            selected.append(vid)
        else:
            if not re.fullmatch(r'\d+(?:-\d+)?', token):
                raise ValueError('번호(1,3), 범위(2-5) 또는 영상 URL을 입력하세요.')
            parts = [int(p) for p in token.split('-')]
            first, last = parts[0], parts[-1]
            if not 1 <= first <= last <= len(entries):
                raise ValueError('표시된 목록 범위 안에서 선택하세요.')
            selected.extend(entries[i - 1]['id'] for i in range(first, last + 1))
    return list(dict.fromkeys(selected))


def playlist_menu(service, choice, read, enrich, output=print):
    try:
        if choice == '4':
            result = service.check()
            if result['baseline']:
                output(f"기준 목록 {result['total']}곡 저장. 기존 곡은 생성하지 않습니다. 이후 4번으로 새 곡을 확인하세요.")
            else:
                output(f"새 곡 {result['new']}개를 대기 목록에 추가했습니다. 노트 생성은 5번에서 선택하세요.")
            return
        if not service.load()['initialized']:
            output('먼저 4번으로 기준 목록을 저장하세요.')
            return
        keyword = read('제목 포함 검색어 (전체: Enter, 취소: c): ').strip()
        if keyword.casefold() == 'c':
            return
        channel = read('채널명 포함 검색어 (전체: Enter): ').strip()
        limit_text = read('최신 몇 곡을 표시할까요? (전체: Enter): ').strip()
        if limit_text and (not limit_text.isdigit() or int(limit_text) < 1):
            raise ValueError('표시 개수는 양의 정수여야 합니다.')
        entries = service.pending(keyword, channel, int(limit_text) if limit_text else None)
        if not entries:
            output('조건에 맞는 대기 곡이 없습니다.')
            return
        for i, entry in enumerate(entries, 1):
            removed = ' / 현재 플레이리스트에서 빠짐' if entry.get('in_playlist') is False else ''
            output(f"{i}. [{entry['status']}] {entry['title']} / {entry['channel']} ({entry['added_at']}){removed}")
        ids = choose_ids(read('노트 생성 번호/범위/URL (예: 1,3 또는 2-4 / 취소: Enter): ').strip(), entries)
        for vid in ids:
            if service.api.cancelled():
                break
            status = service.create(vid, enrich)
            output(f'{vid}: ' + {'completed': '노트 생성·보완 완료', 'existing': '기존 노트가 있어 건너뜀',
                                 'failed': '실패 — 대기 목록에서 선택하여 재시도 가능'}[status])
    except (ValueError, RuntimeError, OSError) as exc:
        output(f'플레이리스트 작업 중단: {exc}')
