import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from note_format import ready, split_note
from playlist_queue import (DEFAULT_PLAYLIST, PlaylistQueue, YouTubeAPI, choose_ids,
                            playlist_id, playlist_menu, render_template, state_lock)

ROOT = Path(__file__).resolve().parents[1]
OLD, NEW, OTHER = 'AAAAAAAAAAA', 'BBBBBBBBBBB', 'CCCCCCCCCCC'


def entry(vid, title='A Cover', channel='Singer', added='2026-10-22T01:00:00Z'):
    return {'id': vid, 'title': title, 'channel': channel, 'added_at': added}


def details(vid):
    return {'id': vid, 'title': 'A "song": [cover] / test', 'channel': 'Singer "name"',
            'channel_id': 'UCchannel', 'published_at': '2026-10-20T00:00:00Z',
            'thumbnail': 'https://i.ytimg.com/vi/' + vid + '/hqdefault.jpg', 'duration': 'PT3M7S'}


class FakeAPI:
    def __init__(self):
        self.items = {OLD: entry(OLD)}
        self.calls = 0
        self.details_calls = 0

    def playlist(self, identifier):
        self.calls += 1
        return self.items

    def details(self, vid):
        self.details_calls += 1
        return details(vid)

    def cancelled(self):
        return False


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = FakeAPI()
        self.service = PlaylistQueue(self.api, DEFAULT_PLAYLIST, self.root/'state',
                                     self.root/'notes', ROOT/'templates/Music.md')

    def add_pending(self):
        self.service.check()
        self.api.items[NEW] = entry(NEW)
        self.service.check()

    def test_baseline_then_new_only_and_restart(self):
        result = self.service.check()
        self.assertTrue(result['baseline'])
        self.assertEqual(self.service.pending(), [])
        self.assertFalse((self.root/'notes').exists())
        self.api.items[NEW] = entry(NEW)
        self.assertEqual(self.service.check()['new'], 1)
        self.assertEqual(self.service.check()['new'], 0)
        again = PlaylistQueue(self.api, DEFAULT_PLAYLIST, self.root/'state', self.root/'notes', ROOT/'templates/Music.md')
        self.assertEqual([r['id'] for r in again.pending()], [NEW])
        self.assertEqual(self.api.details_calls, 0)

    def test_removed_and_readded_is_not_new(self):
        self.add_pending()
        self.api.items = {}
        self.service.check()
        self.assertFalse(self.service.pending()[0]['in_playlist'])
        self.api.items = {OLD: entry(OLD), NEW: entry(NEW)}
        self.assertEqual(self.service.check()['new'], 0)
        self.assertEqual(len(self.service.pending()), 1)

    def test_failed_fetch_does_not_change_state(self):
        self.service.check()
        original = self.service.path.read_bytes()
        with patch.object(self.api, 'playlist', side_effect=RuntimeError('failed page')):
            with self.assertRaises(RuntimeError):
                self.service.check()
        self.assertEqual(self.service.path.read_bytes(), original)

    def test_corrupt_state_is_not_reset(self):
        self.service.path.parent.mkdir()
        self.service.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.service.check()
        self.assertEqual(self.service.path.read_text(), '{broken')
        self.assertEqual(self.api.calls, 0)

    def test_filter_limit_and_explicit_selection(self):
        self.add_pending()
        self.api.items[OTHER] = entry(OTHER, 'Original', 'Other', '2026-10-23T00:00:00Z')
        self.service.check()
        self.assertEqual([r['id'] for r in self.service.pending(limit=1)], [OTHER])
        self.assertEqual([r['id'] for r in self.service.pending('cover', 'sing')], [NEW])
        entries = self.service.pending()
        self.assertEqual(choose_ids('', entries), [])
        self.assertEqual(choose_ids('1,2 1-2', entries), [OTHER, NEW])
        self.assertEqual(choose_ids('https://youtu.be/' + NEW, entries), [NEW])
        for text in ('0', 'all', '1-99999', '3', '2-1', 'https://youtu.be/'+OLD):
            with self.assertRaises(ValueError):
                choose_ids(text, entries)

    def test_template_escaping_and_required_structure(self):
        template = (ROOT/'templates/Music.md').read_text(encoding='utf-8')
        content = render_template(template, details(NEW), playlist_id(DEFAULT_PLAYLIST))
        data = split_note(content)[1]
        self.assertTrue(ready(content))
        self.assertTrue(data['playlist_managed'])
        self.assertEqual(data['Reference'], 'https://www.youtube.com/watch?v='+NEW)
        self.assertIn('A "song": [cover] / test', data['title'])
        self.assertEqual(data['length'], '3:07')
        self.assertIn('![썸네일](https://i.ytimg.com/', content)
        self.assertNotIn('{{', content)
        self.assertNotIn('<%', content)
        from auto_note_updater import is_playlist_managed
        self.assertTrue(is_playlist_managed(content))

    def test_selected_only_and_failed_retry_reuses_note(self):
        self.add_pending()
        enrich = Mock(return_value=False)
        self.assertEqual(self.service.create(NEW, enrich), 'failed')
        path = next((self.root/'notes').glob('*.md'))
        before = path.read_text(encoding='utf-8')
        enrich.return_value = True
        self.assertEqual(self.service.create(NEW, enrich), 'completed')
        self.assertEqual(path.read_text(encoding='utf-8'), before)
        self.assertEqual(len(list((self.root/'notes').glob('*.md'))), 1)
        self.assertEqual(self.api.details_calls, 1)
        self.assertEqual(enrich.call_count, 2)
        self.assertEqual(self.service.pending(), [])
        with self.assertRaises(ValueError):
            self.service.create(NEW, enrich)

    def test_existing_note_is_not_overwritten_or_enriched(self):
        self.add_pending()
        folder = self.root/'notes'
        folder.mkdir()
        path = folder/'my original.md'
        content = '---\nReference: "https://youtu.be/'+NEW+'"\n---\nMy text'
        path.write_text(content, encoding='utf-8')
        enrich = Mock()
        self.assertEqual(self.service.create(NEW, enrich), 'existing')
        enrich.assert_not_called()
        self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_file_collision_never_overwrites(self):
        self.add_pending()
        video = details(NEW)
        import re
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', video['title']).strip(' .')[:100]
        self.service.folder.mkdir()
        path = self.service.folder / f'{name} [{NEW}].md'
        path.write_text('Unrelated content', encoding='utf-8')
        enrich = Mock()
        self.assertEqual(self.service.create(NEW, enrich), 'failed')
        enrich.assert_not_called()
        self.assertEqual(path.read_text(), 'Unrelated content')

    def test_menu_cancel_never_generates(self):
        self.add_pending()
        enrich = Mock()
        read = Mock(side_effect=['', '', '', ''])
        playlist_menu(self.service, '5', read, enrich, output=Mock())
        enrich.assert_not_called()
        self.assertFalse(self.service.folder.exists())

    def test_selected_menu_to_real_note_handler(self):
        import auto_note_updater as app
        self.add_pending()
        app.stop_event.clear()
        handler = app.ObsidianNoteHandler()
        response = 'vocal:: Singer\nalbum:: Test\n[story]\nStory\n[features]\nFeatures\n[lyrics]\nFirst line\n[Chorus]\nSecond line'
        try:
            with patch.object(app, 'get_search_query', return_value='song'), patch.object(app, 'scrape_multiple_sources', return_value='lyrics source'), patch.object(app, 'generate_ai_content', return_value=response):
                playlist_menu(self.service, '5', Mock(side_effect=['cover', 'sing', '1', '1']),
                              handler.update_note, output=Mock())
            path = next(self.service.folder.glob('*.md'))
            content = path.read_text(encoding='utf-8')
            self.assertEqual(split_note(content)[1]['vocal'], 'Singer')
            self.assertIn('[Chorus]\nSecond line', content)
            self.assertEqual(self.service.load()['entries'][NEW]['status'], 'completed')
            self.assertEqual(self.service.pending(), [])
        finally:
            handler.close()
            app.stop_event.clear()

    def test_unavailable_video_stays_pending_for_retry(self):
        self.add_pending()
        enrich = Mock()
        with patch.object(self.api, 'details', side_effect=ValueError('unavailable')):
            self.assertEqual(self.service.create(NEW, enrich), 'failed')
        enrich.assert_not_called()
        self.assertEqual(list(self.service.folder.glob('*.md')), [])
        self.assertEqual(self.service.pending()[0]['status'], 'failed')

    def test_os_lock_blocks_competing_writer(self):
        with state_lock(self.service.path):
            with self.assertRaises(RuntimeError):
                self.service.check()
        self.assertTrue(self.service.check()['baseline'])


class APItests(unittest.TestCase):
    def response(self, data, status=200):
        return Mock(status_code=status, json=Mock(return_value=data))

    def test_pagination_and_duplicate_video(self):
        row = {'snippet': {'resourceId': {'videoId': NEW}, 'title': 'Cover', 'publishedAt': '2026-10-22'}}
        get = Mock(side_effect=[self.response({'items': [row], 'nextPageToken': 'next'}),
                                self.response({'items': [row]})])
        api = YouTubeAPI('secret', get=get)
        self.assertEqual(list(api.playlist('PLtest')), [NEW])
        self.assertEqual(get.call_args.kwargs['params']['pageToken'], 'next')
        self.assertEqual(get.call_args.kwargs['timeout'], 30)

    def test_errors_do_not_expose_key(self):
        for get in (Mock(side_effect=requests.ConnectionError('url?key=secret')),
                    Mock(return_value=self.response({'error': 'secret'}, 403))):
            with self.assertRaises(RuntimeError) as caught:
                YouTubeAPI('secret', get=get).playlist('PLtest')
            self.assertNotIn('secret', str(caught.exception))

    def test_repeated_token_is_failure(self):
        get = Mock(return_value=self.response({'items': [], 'nextPageToken': 'same'}))
        with self.assertRaises(RuntimeError):
            YouTubeAPI('secret', get=get).playlist('PLtest')


if __name__ == '__main__':
    unittest.main()
