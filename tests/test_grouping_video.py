"""分组（groups.json）与视频资源复制的测试：单元 + 轻量集成。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('site_init', ROOT / 'scripts/init.py')
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class GroupHelpersTest(unittest.TestCase):
    def test_read_groups_valid(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            src = Path(tmp)
            (src / 'groups.json').write_text(
                json.dumps({'groups': [
                    {'name': '初中', 'chapters': ['A', 'B']},
                    {'name': '高中', 'chapters': ['C']},
                ]}, ensure_ascii=False), encoding='utf-8')
            groups = GENERATOR.read_groups(str(src))
            self.assertEqual(groups, [
                {'name': '初中', 'chapters': ['A', 'B']},
                {'name': '高中', 'chapters': ['C']},
            ])

    def test_read_groups_missing(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            self.assertIsNone(GENERATOR.read_groups(tmp))

    def test_ordered_chapters(self):
        chapters = ['A', 'B', 'C', 'D']
        groups = [
            {'name': '高中', 'chapters': ['C']},
            {'name': '初中', 'chapters': ['B', 'A']},
        ]
        self.assertEqual(GENERATOR.ordered_chapters(chapters, groups),
                         ['C', 'B', 'A', 'D'])

    def test_to_sidebar_item(self):
        self.assertEqual(
            GENERATOR.to_sidebar_item({'type': 'single', 'text': 'x', 'link': '/x/'}),
            {'text': 'x', 'link': '/x/'})
        self.assertEqual(
            GENERATOR.to_sidebar_item({'type': 'group', 'text': 'y', 'items': [{'text': 'z', 'link': '/z/'}]}),
            {'text': 'y', 'collapsed': True, 'items': [{'text': 'z', 'link': '/z/'}]})

    def test_first_leaf_link(self):
        self.assertEqual(GENERATOR.first_leaf_link({'text': 'x', 'link': '/x/'}), '/x/')
        self.assertEqual(
            GENERATOR.first_leaf_link({'type': 'group', 'text': 'y', 'items': [
                {'text': 'z', 'link': '/z/'}, {'text': 'w', 'link': '/w/'}]}),
            '/z/')


class GroupingVideoIntegrationTest(unittest.TestCase):
    def test_group_and_video_copy(self):
        with tempfile.TemporaryDirectory(prefix='md2website-gv-', dir='/tmp') as tmp:
            root = Path(tmp)
            src = root / 'source'
            (src / '初中A').mkdir(parents=True)
            (src / '高中B').mkdir(parents=True)
            (src / 'groups.json').write_text(json.dumps({'groups': [
                {'name': '初中', 'chapters': ['初中A']},
                {'name': '高中', 'chapters': ['高中B']},
            ]}, ensure_ascii=False), encoding='utf-8')
            (src / '初中A' / '初中A.md').write_text('# A\n正文\n<video src="v.mp4"></video>\n', encoding='utf-8')
            (src / '初中A' / 'v.mp4').write_bytes(b'\x00' * 16)
            (src / '高中B' / '高中B.md').write_text('# B\n正文\n', encoding='utf-8')

            out = root / 'out'
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'scripts' / 'init.py'),
                 str(src), '--out', str(out), '--title', '测试'],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # 视频被复制
            self.assertTrue((out / 'docs' / '初中A' / 'v.mp4').is_file())
            # 侧边栏按分组输出，URL 用章节 slug
            cfg = (out / 'docs' / '.vitepress' / 'config.mjs').read_text(encoding='utf-8')
            self.assertIn('"type": "group", "text": "初中"', cfg)
            self.assertIn('"type": "group", "text": "高中"', cfg)
            self.assertIn('"link": "/初中A/"', cfg)
            # 首页 tagline 带分组计数
            home = (out / 'docs' / 'index.md').read_text(encoding='utf-8')
            self.assertIn('共 2 个章节', home)
            self.assertIn('初中 1', home)


if __name__ == '__main__':
    unittest.main()
