import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from note_format import (STR_KEYS, LIST_KEYS, atomic_save, extract_property,
                         extract_section, ready, render_note, song_title, split_note)


def template():
    return ('---\nReference: "https://www.youtube.com/watch?v=123"\n'
            'title: "[Actual song](https://www.youtube.com/watch?v=123)"\n'
            'tags:\n  - Music/Channel\n'
            + ''.join(k + ':\n' for k in STR_KEYS + LIST_KEYS)
            + 'cssclasses: lyrics-center\n---\n'
            '- **핵심 서사**\n  \n- **음악적 특징**\n\n## 🎤 가사\n---\n'
            '``` title="Actual song"\n\n```\n')


class NoteTests(unittest.TestCase):
    def test_template_readiness(self):
        self.assertTrue(ready(template()))
        self.assertFalse(ready(template().replace('Actual song', '{{title}}')))
        self.assertFalse(ready(template().replace('Actual song', '<% tp.file.title %>')))
        self.assertFalse(ready('ordinary note'))

    def test_title_priority(self):
        self.assertEqual(song_title(template(), 'renamed.md'), 'Actual song')

    def test_empty_property(self):
        self.assertEqual(extract_property('tags', 'tags:: \n\n[story]\ntext'), '')

    def test_stanza_labels(self):
        self.assertEqual(extract_section('lyrics', '[lyrics]\nfirst\n[Chorus]\nsecond'), 'first\n[Chorus]\nsecond')

    def test_preserve_and_quote(self):
        source = template().replace('composer:\n', 'composer:\n  - Existing\n')
        result = render_note(source, 'composer:: New\nalbum:: The "Best"\ndescription:: C:\\Music\ntags:: Music/Channel, New\n[lyrics]\nfirst\n[Chorus]\nsecond')
        _, data = split_note(result)
        self.assertEqual(data['composer'], ['Existing'])
        self.assertEqual(data['album'], 'The "Best"')
        self.assertEqual(data['description'], 'C:\\Music')
        self.assertEqual(data['tags'], ['Music/Channel', 'New'])
        self.assertIn('[Chorus]\nsecond', result)
        self.assertIn('cssclasses: lyrics-center', result)

    def test_lyrics_modes(self):
        source = template().replace('\n\n```', '\nold lyrics\n```')
        response = 'album:: Changed\n[lyrics]\nnew lyrics'
        self.assertIn('old lyrics', render_note(source, response))
        result = render_note(source, response, lyrics_only=True)
        self.assertIn('new lyrics', result)
        self.assertIsNone(split_note(result)[1]['album'])
        self.assertEqual(render_note(source, '[lyrics]\n', True), source)

    def test_atomic_conflict_and_failure(self):
        # Local workspace keeps the test independent of Windows sandbox TEMP settings.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / 'note.md'
            path.write_bytes(b'original')
            atomic_save(path, b'original', b'updated')
            self.assertEqual(path.read_bytes(), b'updated')
            with self.assertRaises(ValueError):
                atomic_save(path, b'original', b'overwrite')
            with patch('note_format.os.replace', side_effect=OSError('locked')):
                with self.assertRaises(OSError):
                    atomic_save(path, b'updated', b'new')
            self.assertEqual(path.read_bytes(), b'updated')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_handler_preserves_edit_during_generation(self):
        import auto_note_updater as app
        app.stop_event.clear()
        handler = app.ObsidianNoteHandler()
        try:
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                path = Path(directory) / 'note.md'
                path.write_text(template(), encoding='utf-8')
                edited = template() + '\nUser edit\n'
                def generate(*args):
                    path.write_text(edited, encoding='utf-8')
                    return 'album:: New album'
                with patch.object(app, 'get_search_query', return_value='song'), patch.object(app, 'scrape_multiple_sources', return_value='source'), patch.object(app, 'generate_ai_content', side_effect=generate):
                    self.assertFalse(handler.update_note(str(path)))
                self.assertEqual(path.read_text(encoding='utf-8'), edited)
        finally:
            handler.close()
            app.stop_event.clear()

    def test_handler_no_sources_leaves_original(self):
        import auto_note_updater as app
        app.stop_event.clear()
        handler = app.ObsidianNoteHandler()
        try:
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                path = Path(directory) / 'note.md'
                path.write_text(template(), encoding='utf-8')
                with patch.object(app, 'get_search_query', return_value='song'), patch.object(app, 'scrape_multiple_sources', return_value=''), patch.object(app, 'generate_ai_content') as generate:
                    self.assertFalse(handler.update_note(str(path)))
                    generate.assert_not_called()
                self.assertEqual(path.read_text(encoding='utf-8'), template())
        finally:
            handler.close()
            app.stop_event.clear()


if __name__ == '__main__':
    unittest.main()
