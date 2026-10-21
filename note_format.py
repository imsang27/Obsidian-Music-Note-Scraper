"""Music.md parsing and conflict-aware, atomic persistence (no API calls)."""
import os
import re
import tempfile
from pathlib import Path

import yaml

STR_KEYS = ('vocal', 'group', 'album', 'original_song', 'original_artist', 'description')
LIST_KEYS = ('lyricist', 'composer', 'arranger', 'genre', 'mood')
LYRICS = re.compile(r'(^``` title="[^\n]*"[ \t]*\n)(.*?)(^```[ \t]*(?=\n|$))', re.M | re.S)


def split_note(content):
    match = re.match(r'\A(\ufeff?---\n)(.*?)(^---[ \t]*\n)(.*)\Z', content, re.M | re.S)
    if not match:
        raise ValueError('Frontmatter를 찾을 수 없습니다.')
    data = yaml.safe_load(match[2])
    if not isinstance(data, dict):
        raise ValueError('Frontmatter는 속성 목록이어야 합니다.')
    return match, data


def ready(content):
    if re.search(r'\{\{.*?\}\}|<%', content, re.S):
        return False
    try:
        match, data = split_note(content)
        return (all(k in data for k in STR_KEYS + LIST_KEYS)
                and bool(data.get('Reference'))
                and '- **핵심 서사**' in match[4]
                and '- **음악적 특징**' in match[4]
                and '## 🎤 가사' in match[4] and bool(LYRICS.search(match[4])))
    except (ValueError, yaml.YAMLError):
        return False


def song_title(content, path):
    _, data = split_note(content)
    title = str(data.get('title') or Path(path).stem).strip()
    link = re.fullmatch(r'\[(.*)\]\(https?://.*\)', title)
    if link:
        title = link[1]
    return re.sub(r'^\[.*?\]\s*', '', title).strip()


def extract_property(key, text):
    match = re.search(rf'^{re.escape(key)}::[ \t]*([^\r\n]*)', text, re.M)
    value = match[1].strip() if match else ''
    return '' if '::' in value else value


def extract_section(tag, text):
    match = re.search(rf'^\[{re.escape(tag)}\][ \t]*\n(.*?)(?=^\[(?:story|features|lyrics)\][ \t]*$|\Z)', text, re.M | re.S)
    return match[1].strip() if match else ''


def render_note(content, response, lyrics_only=False):
    match, data = split_note(content)
    header, body = match[2], match[4]
    if not lyrics_only:
        changes = {}
        for key in STR_KEYS + LIST_KEYS:
            value = extract_property(key, response)
            if key in data and data[key] in (None, '', []) and value:
                changes[key] = [v.strip() for v in value.split(',') if v.strip()] if key in LIST_KEYS else value
        tags = [re.sub(r'^[-#\s]+', '', v.strip()).replace(' ', '_')
                for v in extract_property('tags', response).split(',') if v.strip()]
        old_tags = data.get('tags')
        if tags and (old_tags is None or isinstance(old_tags, (list, str))):
            existing = old_tags if isinstance(old_tags, list) else ([old_tags] if old_tags else [])
            merged = list(dict.fromkeys(existing + tags))
            if merged != existing:
                changes['tags'] = merged
        # Replace only changed top-level fields; preserve dates, comments and links elsewhere.
        for key, value in changes.items():
            pattern = rf'^{key}:[^\n]*(?:\n(?![^ \t\n#][^\n]*:)[^\n]*)*\n?'
            replacement = yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False, width=10000)
            header, count = re.subn(pattern, lambda m: replacement, header, count=1, flags=re.M)
            if not count:
                header += replacement
        for tag, label, following in [('story', '핵심 서사', '- **음악적 특징**'), ('features', '음악적 특징', '## 🎤 가사')]:
            pattern = rf'(^- \*\*{label}\*\*\n)(.*?)(?=^{re.escape(following)})'
            value = extract_section(tag, response)
            def fill(m):
                if m[2].strip() or not value:
                    return m[0]
                return m[1] + '\n'.join('  ' + line.lstrip('-* ') for line in value.splitlines() if line.strip()) + '\n\n'
            body = re.sub(pattern, fill, body, flags=re.M | re.S)
    lyrics = extract_section('lyrics', response)
    if lyrics and not re.search(r'^```', lyrics, re.M):
        body = LYRICS.sub(lambda m: m[1] + lyrics + '\n' + m[3] if lyrics_only or not m[2].strip() else m[0], body, count=1)
    result = match[1] + header + match[3] + body
    split_note(result)  # Never persist invalid YAML.
    return result


def atomic_save(path, original, updated):
    """Detect edits during generation; stage on the same volume before replacement."""
    path = Path(path)
    if path.read_bytes() != original:
        raise ValueError('작업 중 파일이 변경되어 저장을 중단했습니다. 수동 복구로 다시 시도하세요.')
    if updated == original:
        return
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise ValueError('저장 직전 파일 변경을 감지했습니다.')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
