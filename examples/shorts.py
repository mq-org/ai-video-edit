"""架空の対談を想定したType-T設計例。実素材に合わせて値を変更する。

このファイルを data/projects/demo/shorts.py にコピーして編集する。
python scripts/typet_shorts.py data/projects/demo/shorts.py ass
Python specはコードとして実行されるため、自分で確認したものだけを使う。
"""
from pathlib import Path

# examples/ と data/projects/demo/ のどちらでもリポジトリ直下を探す。
BASE = next(p for p in Path(__file__).resolve().parents if (p / 'scripts/typet_shorts.py').is_file())
SRC = BASE / 'data/source/demo.mp4'
WHISPER = BASE / 'data/projects/demo/transcript/whisper_verbose.json'
OUT_DIR = BASE / 'output/demo-shorts'
GLOSSARY = BASE / 'data/projects/demo/glossary.tsv'
REVIEWED = False  # 素材・カット・台本を確認してからTrueにする
RESOLVE_CUTS = True
LAYOUTS = {'wide': ('crop=1280:620:0:0', 1080, 522)}
CLIPS = [
    dict(id='clip01', title='小さな一歩で/変わる毎日', layout='wide',
         segs=[(10.0, 28.0), (35.0, 55.0)],
         telops=[
             (10.1, 'Neutral', '新しいことを/始めたいとき'),
             (15.0, 'PainBlue', '失敗するのが/怖いですよね'),
             (21.0, 'Connector', 'どうすればいい？'),
             (35.1, 'Yellow', 'まず小さなことから/試してみる'),
             (43.0, 'Gold', '[小さな一歩]'),
             (49.0, 'Neutral', '自分のペースで/続けていこう'),
         ]),
]
