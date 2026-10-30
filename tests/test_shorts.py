"""既存ショート機能を一般公開向けに移す際の回帰テスト。"""
import os
os.environ['NO_VENV'] = '1'
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import typet_shorts as ts
from fix_transcript import apply, find_in_words

class ShortsRegressionTests(unittest.TestCase):
    def test_reordered_source_mapping(self):
        mapping, total = ts.make_mapper([(30, 40, None), (5, 15, None)])
        self.assertEqual(total, 20)
        self.assertEqual(mapping(35), 5)
        self.assertEqual(mapping(7), 12)

    def test_more_than_four_box_labels(self):
        spec = SimpleNamespace(SRC=Path('unused.mp4'), WHISPER=Path('missing.json'), RESOLVE_CUTS=False,
                               LAYOUTS={'wide': ('crop=1280:620:0:0', 1080, 522)})
        clip = dict(id='test', title='テスト', layout='wide', segs=[(0, 35)],
                    telops=[(i * 5, 'BoxBlue', f'{i + 1}つ目') for i in range(7)])
        ass, total, events, warns = ts.build_ass(clip, spec)
        self.assertEqual(len(events), 7)
        self.assertEqual(total, 35)
        self.assertIn('7つ目', ass)

    def test_per_project_glossary(self):
        with tempfile.TemporaryDirectory() as directory:
            glossary = Path(directory) / 'terms.tsv'
            glossary.write_text('校正\tこうせい\t公正\t文章\n', encoding='utf-8')
            self.assertTrue(ts.glossary_warnings(['公正する'], 'test', SimpleNamespace(GLOSSARY=glossary)))

    def test_transcript_word_replacement_keeps_timing(self):
        words = [{'word': '公', 'start': 1, 'end': 2}, {'word': '正', 'start': 2, 'end': 3}]
        hit = find_in_words(words, '公正')[0]
        apply(words, hit, '公正', '校正')
        self.assertEqual(''.join(w['word'] for w in words), '校正')
        self.assertEqual(words[0]['start'], 1)
        self.assertEqual(words[-1]['end'], 3)

if __name__ == '__main__':
    unittest.main()
