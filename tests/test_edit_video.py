"""編集時刻・保護条件・実際の音声付き書き出しを確認する。"""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import edit_video as editor
import plan_edit as planner


class TimelineTests(unittest.TestCase):
    def test_captions_follow_reordered_and_repeated_material(self):
        segments = editor.quantize_segments([
            {'start': 10, 'end': 12}, {'start': 0, 'end': 2}, {'start': 10, 'end': 12}], 30)
        captions = editor.map_captions([
            {'start': 0.5, 'end': 1.5, 'text': '最初'},
            {'start': 10.5, 'end': 11.5, 'text': '後半'},
            {'start': 5, 'end': 6, 'text': '除外される字幕'}], segments, 12)
        self.assertEqual([c['text'] for c in captions], ['後半', '最初', '後半'])
        self.assertEqual([(c['start'], c['end']) for c in captions], [(0.5, 1.5), (2.5, 3.5), (4.5, 5.5)])

    def test_caption_is_clipped_at_both_cut_edges(self):
        segments = editor.quantize_segments([{'start': 2, 'end': 3}], 30)
        self.assertEqual(editor.map_captions([{'start': 1, 'end': 4, 'text': '話'}], segments, 12),
                         [{'start': 0.0, 'end': 1.0, 'text': '話'}])

    def test_automatic_captions_never_join_across_cut(self):
        words = [{'start': 0.1, 'end': 0.9, 'word': '前の文'}, {'start': 4.1, 'end': 4.9, 'word': '別の文'}]
        segments = editor.quantize_segments([{'start': 0, 'end': 1}, {'start': 4, 'end': 5}], 30)
        captions = editor.captions_from_words(words, segments, 12)
        self.assertEqual([c['text'] for c in captions], ['前の文', '別の文'])
        self.assertAlmostEqual(captions[1]['start'], 1.1)

    def test_zero_duration_whisper_words_are_kept(self):
        words = editor.validate_intervals([
            {'start': 0, 'end': 0, 'word': 'ここ'},
            {'start': 0, 'end': 0.7, 'word': 'から'},
            {'start': 0.7, 'end': 0.7, 'word': '説明'},
            {'start': 0.8, 'end': 1.5, 'word': 'します'},
            {'start': 1.5, 'end': 1.5, 'word': '。'}], 2, 'words', allow_zero=True)
        captions = editor.captions_from_words(words, editor.quantize_segments([{'start': 0, 'end': 2}], 30), 26)
        self.assertEqual(''.join(c['text'].replace('\n', '') for c in captions), 'ここから説明します。')
        self.assertTrue(all(c['end'] > c['start'] for c in captions))
        isolated = editor.captions_from_words([{'start': 0.5, 'end': 0.5, 'word': 'はい'}],
                                              editor.quantize_segments([{'start': 0, 'end': 2}], 30), 26)
        self.assertEqual(isolated[0]['text'], 'はい')
        self.assertGreater(isolated[0]['end'], isolated[0]['start'])

    def test_whisper_overlap_keeps_all_text_without_overlapping_captions(self):
        words = [
            {'start': 0.2, 'end': 1.0, 'word': '前'},
            {'start': 1.0, 'end': 1.0, 'word': '、'},
            {'start': 1.0, 'end': 2.0, 'word': '後。'},
            {'start': 1.7, 'end': 2.1, 'word': '次。'}]
        captions = editor.captions_from_words(words, editor.quantize_segments([{'start': 0, 'end': 3}], 30), 26)
        self.assertEqual(''.join(c['text'].replace('\n', '') for c in captions), '前、後。次。')
        self.assertTrue(all(c['start'] >= p['end'] for p, c in zip(captions, captions[1:])))
        self.assertTrue(any(c.get('timing_adjusted') for c in captions))

    def test_no_deletion_without_audio_silence(self):
        words = [{'start': 0, 'end': 1}, {'start': 9, 'end': 10}]
        self.assertEqual(planner.propose_removals(words, [], 10), [])

    def test_no_deletion_when_transcript_contains_speech(self):
        self.assertEqual(planner.propose_removals([{'start': 0, 'end': 10}], [(1, 9)], 10), [])

    def test_silence_removal_preserves_onset_and_offset_guards(self):
        words = [{'start': 0, 'end': 2}, {'start': 6, 'end': 8}]
        cuts = planner.propose_removals(words, [(1, 7)], 8)
        self.assertEqual(len(cuts), 1)
        self.assertGreaterEqual(cuts[0]['start'], 2.18)
        self.assertLessEqual(cuts[0]['end'], 5.88)
        self.assertAlmostEqual(cuts[0]['start'], 2.28)
        self.assertAlmostEqual(cuts[0]['end'], 5.78)

    def test_nonfinite_and_backwards_times_rejected(self):
        for interval in [{'start': float('nan'), 'end': 3}, {'start': 4, 'end': 3}, {'start': True, 'end': 3}]:
            with self.assertRaises(ValueError):
                editor.validate_intervals([interval], 10, 'test')

    def test_ass_control_text_is_literal(self):
        escaped = editor.ass_escape('{\\pos(1,1)}Hello\nWorld')
        self.assertNotIn('{', escaped)
        self.assertNotIn('\\pos', escaped)
        self.assertIn('\\N', escaped)

    def test_timecode_rounding_carries_to_next_minute(self):
        self.assertEqual(editor.timecode(59.9999), '00:01:00,000')
        self.assertEqual(editor.timecode(59.9999, ass=True), '0:01:00.00')


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe') or not editor.FONT_PATH.exists():
            raise unittest.SkipTest('FFmpeg/同梱フォントが必要です')
        try:
            import PIL
        except ImportError:
            raise unittest.SkipTest('Pillowが必要です')
        cls.temp = tempfile.TemporaryDirectory(prefix="video test ' : ")
        cls.directory = Path(cls.temp.name)
        cls.source = cls.directory / "source ' : , [].mp4"
        editor.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30:duration=8',
                    '-f', 'lavfi', '-i', r'aevalsrc=if(between(t\,2\,4)\,0\,0.2*sin(2*PI*440*t)):s=48000:d=8',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-shortest', cls.source])
        cls.transcript = cls.directory / 'words.json'
        cls.transcript.write_text(json.dumps({'words': [
            {'start': 0.2, 'end': 1.8, 'word': '最初の説明'},
            {'start': 4.2, 'end': 5.8, 'word': '次の説明'},
            {'start': 6.0, 'end': 7.8, 'word': 'まとめ'}]}, ensure_ascii=False))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def make_plan(self, name):
        path = self.directory / f'{name}.json'
        plan = planner.build_plan(self.source, self.transcript, path, output=self.directory / f'{name}.mp4')
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        return path, plan

    def test_full_pipeline_with_real_silence_and_quoted_paths(self):
        path, plan = self.make_plan('clean')
        self.assertEqual(len(plan['removed']), 1)
        self.assertAlmostEqual(plan['removed'][0]['start'], 2.1, delta=0.03)
        self.assertAlmostEqual(plan['removed'][0]['end'], 3.9, delta=0.03)
        plan['reviewed'] = True
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        config = editor.load_plan(path)
        editor.render(config)
        info = editor.probe(self.directory / 'clean.mp4')
        self.assertAlmostEqual(info['duration'], 6.2, delta=0.12)
        self.assertEqual((info['width'], info['height'], info['channels']), (320, 180, 2))
        srt = (self.directory / 'clean.srt').read_text()
        self.assertIn('最初の説明', srt)
        self.assertIn('次の説明', srt)
        with self.assertRaises(ValueError):
            editor.render(config)

    def test_sidecar_subtitles_without_burning_into_video(self):
        from unittest.mock import patch
        path, plan = self.make_plan('sidecar')
        plan['reviewed'] = True
        plan['subtitle']['burn_in'] = False
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        config = editor.load_plan(path, preview=1.5)
        # 実際にレンダーしつつ、最終FFmpegに字幕焼き込みがないことも確認する。
        with patch.object(editor, 'run', wraps=editor.run) as calls:
            editor.render(config)
        commands = [str(x) for call in calls.call_args_list for x in call.args[0]]
        self.assertFalse(any('ass=filename=' in x for x in commands))
        self.assertIn('最初の説明', config['output'].with_suffix('.srt').read_text())
        report = json.loads(config['output'].with_suffix('.report.json').read_text())
        self.assertFalse(report['subtitle_burned_in'])
        self.assertGreater(report['caption_count'], 0)

    def test_review_required_and_source_cannot_be_overwritten(self):
        path, plan = self.make_plan('guard')
        with self.assertRaisesRegex(ValueError, 'reviewed'):
            editor.render(editor.load_plan(path))
        plan['output'] = str(self.source)
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, '上書き'):
            editor.load_plan(path)

    def test_preview_keeps_full_output_separate(self):
        path, plan = self.make_plan('previewtest')
        plan['reviewed'] = True
        path.write_text(json.dumps(plan))
        config = editor.load_plan(path, preview=1.5)
        editor.render(config)
        self.assertEqual(config['output'].name, 'previewtest.preview.mp4')
        self.assertFalse((self.directory / 'previewtest.mp4').exists())
        self.assertAlmostEqual(editor.probe(config['output'])['duration'], 1.5, delta=0.1)


if __name__ == '__main__':
    unittest.main()
