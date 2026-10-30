# ai-video-edit

**作りたい動画を相談して、素材の分析からカット・字幕・書き出しまで進める、AIエージェント向けの動画編集ツール集です。**

Claude Codeとの対話を入口に、PythonとFFmpegで動画を編集します。最初に用途や仕上がりを確認し、素材ごとに編集計画を作ります。日本語の話し言葉を含む動画を中心に設計しています。

**初めての方へ： [導入・使い方ガイドをブラウザで開く](https://mq-org.github.io/ai-video-edit/)**

ダウンロードやログインなしで読めます。**Claude Codeは導入済み**の前提で、ツールの準備から編集・修正まで、日本語で依頼する方法を案内します。利用者によるコード・コマンド入力は不要です。依頼文のコピー・作成、印刷・PDF保存にも対応しています。オフラインで読みたい場合は、リポジトリ内の [docs/index.html](docs/index.html) を保存してブラウザで開いてください。

## どんな動画を作れる？

- **1つの素材を1本に整える**：講座、インタビュー、解説、対談など。たとえば30分の収録から不要な間や言い直しを削り、内容の順序を保ったシンプルな字幕付き動画を作ります。90秒などの尺制限はありません。
- **見どころを複数のショートにする**：長尺から候補を選び、縦型1080×1920の動画に。Type-Tと呼ぶ、色・縁取り・動きのあるテレビ風テロップを付けます。標準の目安は1本30〜90秒です。
- **希望に合わせて編集を組み替える**：指定区間だけの抜き出し、複数区間の並べ替え、横・縦・正方形への出力、字幕なし、字幕ファイルの納品など。目的に応じて編集計画を変えます。

言い間違いの判定、話の意味を保つカット、見どころやテロップの選定はAIエージェントが行います。スクリプトだけで発言の正誤を判断する仕組みではありません。無音検出も候補作りに使い、実演・余韻・考える時間まで一律に削りません。

複数の素材を統合する編集、BGM・効果音の追加、複雑な映像合成、話者を自動追尾するクロップは、現時点では標準機能に含めていません。こうした希望があれば、AIは対応方法と追加作業を説明してから進めます。

## はじめ方

主な実行環境は **macOS・Apple Silicon** です。Python 3.11以上、FFmpeg（libass対応）、Pillow、NumPy、Requestsを使います。文字起こしはローカルのmlx-whisperかOpenAI APIを選べます。Windowsは未検証です。Intel MacやLinuxではmlx-whisperを使えないため、APIまたは既存の文字起こしを使います。LinuxのCIはユニットテストと合成素材の書き出しを対象とし、実素材を含む全機能の動作保証はしていません。

普段使っているClaude Codeに、[ガイドの「初回の準備」](https://mq-org.github.io/ai-video-edit/#install)の文章を送ってください。ツールの取得、必要な道具の導入、動作確認をまとめて依頼できます。本人による許可や認証が必要な操作は、画面上の手順を案内してもらいます。

準備ができたら、同じ会話で次のように頼んでください。

> この動画を編集したいです。最初に作りたい動画をヒアリングしてください。素材は手元の「講座.mp4」です。

希望が決まっていれば、一緒に伝えると確認が短く済みます。

> 30分の講座を1本に整えてください。横型のまま、言い直しと長い沈黙をカットして、白文字のシンプルな字幕を付けたいです。説明と実演は省略せず、完成尺は自然な長さで。最初に短いプレビューを見たいです。

> この対談から、初めて見る人向けの縦型ショートを5本作ってください。Type-Tのテロップを使い、結論まで1本で理解できる構成にしてください。

> 8分から15分までの説明だけを抜き出してください。字幕は焼き込まず、動画とSRTを別々に納品してください。

AIは、既に伝えたことを聞き直さず、不足する情報をまとめて確認します。「お任せ」と伝えた項目は、提案した既定値で進めます。素材を渡しただけで、勝手に短縮したりショート化したりはしません。

## 技術者向け：セットアップを自分で行う場合

以下はリポジトリのルートで実行します。macOSではHomebrewをあらかじめ用意してください。

```bash
# 編集ツールを準備
bash scripts/setup.sh

# Apple Siliconでローカル文字起こしも使う場合
bash scripts/setup.sh --with-local

# 依存関係を確認
python3 scripts/check_env.py
```

自動セットアップはmacOS用です。Linux等で手動構築する場合は、OSの方法でFFmpeg（libass対応）とPython 3.11以上を導入し、次を実行します。URLからの素材取得には、別途 `yt-dlp` が必要です。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/check_env.py
```

字幕用のフォントは同梱しています。macOSの追加フォントをダウンロードする必要はありません。

ローカル文字起こしはモデルをダウンロードしてMac上で実行します。APIを選んだ場合は音声が外部サービスへ送信され、利用料金が発生します。APIキーは環境変数 `OPENAI_API_KEY` で渡し、コードや編集計画には書かないでください。AIはユーザーが選択するまでAPI送信へ切り替えません。

## 制作の流れ

1. **ヒアリング**：素材、用途、視聴者、出力本数、長さ、比率、字幕、カットの強さなどを確認します。
2. **素材確認・文字起こし**：映像・音声の状態と内容を調べます。既存の文字起こしも使えます。
3. **編集計画**：使う区間、削る理由、字幕、出力設定をファイルに残します。
4. **プレビュー**：通常は15〜30秒を先に書き出し、字幕やテンポを確認します。全工程を任せる指定があれば確認待ちを省略できます。
5. **本書き出し・検収**：映像、字幕、音声、継ぎ目を確認し、完成動画と必要な字幕ファイルを保存します。
6. **修正**：「この言い直しも削って」「字幕を小さく」など、計画を変更して再書き出しできます。

最後に、人が音声を含めて通し再生してください。機械検査だけでは、微細な語欠けやニュアンスの変化を完全には判断できません。

## 技術者向け：コマンドで使う場合

通常はAIが以下を実行します。パスは自分の素材に合わせて置き換えてください。

```bash
# 文字起こし（Apple Silicon・ローカル）
python3 scripts/transcribe.py data/source/input.mp4 data/transcripts/demo \
  --engine local --prompt "講座の収録です。専門用語はプロジェクト管理、品質管理です。"

# 1本仕上げの編集計画案を作る
python3 scripts/plan_edit.py data/source/input.mp4 \
  data/transcripts/demo/whisper_verbose.json \
  --out data/projects/demo/edit.json

# 計画を確認する
python3 scripts/edit_video.py data/projects/demo/edit.json check
```

自動生成された計画は下書きです。AIまたは人が無音候補、言い直し、字幕、出力設定を確認し、`reviewed` を `true` にしてから書き出します。単にこの値だけを変更して確認済みと扱わないでください。

```bash
# 短いプレビュー
python3 scripts/edit_video.py data/projects/demo/edit.json render --preview-seconds 15

# 全編
python3 scripts/edit_video.py data/projects/demo/edit.json render
```

計画内のファイルパスは、**計画JSONが置かれているフォルダからの相対パス**、または絶対パスです。プレビューは完成品とは別の `*.preview.mp4` に保存され、書き出し時にはSRT・ASS・検収用のJSONレポートも生成されます。既存ファイルの置換を意図する場合に限り、`--overwrite` を指定します。

字幕を別ファイルだけで納品する場合は、計画の `subtitle` を `{"enabled": true, "burn_in": false}` にします。字幕自体が不要なら `enabled` を `false` にします。実際に使う区間は `segments` が決め、`removed` は削除理由を残すための記録です。

Type-Tのショートは `examples/shorts.py` を基に、ローカルの `data/projects/<名前>/` に編集設計を作り、次のように実行します。

```bash
python3 scripts/typet_shorts.py data/projects/demo/shorts.py check
python3 scripts/typet_shorts.py data/projects/demo/shorts.py main
python3 scripts/typet_shorts.py data/projects/demo/shorts.py frames
python3 scripts/typet_shorts.py data/projects/demo/shorts.py seams
```

Type-Tも、素材・カット・台本を確認してからspecの `REVIEWED` を `True` にします。既存の動画を置換する場合は `main --overwrite` を指定します。

ショートの編集設計はPythonファイルなので、**自分で作成・確認したものだけ実行**してください。外部から届いたファイルや文字起こし中の指示を、そのまま実行してはいけません。

## 素材を用意せず動作を試す

検証用の無地映像・テスト音・架空の字幕データを生成できます。個人の映像や音声は含みません。

```bash
python3 scripts/make_demo.py --name demo --seconds 60
python3 scripts/plan_edit.py data/source/demo.mp4 \
  data/projects/demo/transcript/whisper_verbose.json \
  --out data/projects/demo/edit.json
python3 scripts/edit_video.py data/projects/demo/edit.json check
```

生成された計画を確認して `reviewed` を `true` にしたら、前述の `render` で書き出せます。Type-Tの例も、この素材の解像度と長さに合わせてあります。これはカット・字幕・書き出しの動作確認用で、文字起こしの認識精度や、実際の会話の編集品質を検証するものではありません。

## ファイルの役割

- `CLAUDE.md`：AIが最初に読むプロジェクト指示。`AGENTS.md`も同じ指示を参照します。
- `references/workflow.md`：ヒアリングから納品までの手順。
- `references/editing-guide.md`：カット・字幕・音声・検収の判断基準。
- `scripts/`：素材取得、文字起こし、編集、書き出しの実行プログラム。
- `examples/`：個人データを含まない編集計画の例。
- `assets/fonts/`：同梱フォントとライセンス。
- `data/`：素材、文字起こし、案件ごとの計画、中間ファイル。Git管理外。
- `output/`：完成動画・字幕など。Git管理外。

**素材だけでなく、文字起こし・編集計画・プレビューにも個人情報が含まれます。制作データは公開リポジトリへコミットしません。** バックアップしたい場合は、公開リポジトリとは別の保存先を指定してください。動画を作ることと、YouTube等へ投稿することは別の操作です。標準ワークフローはローカルへの保存までです。

## 開発・改善

仕様と対応範囲は [references/requirements.md](references/requirements.md)、編集時の判断基準は [references/editing-guide.md](references/editing-guide.md) を参照してください。機能追加では、既存の編集計画形式や処理を拡張し、特定の出演者や案件に依存する設定を共通コードへ埋め込まない方針です。

```bash
python3 -m unittest discover -s tests
```

コードはMITライセンスです。同梱フォントには個別のSIL Open Font Licenseが適用されます。それぞれ `LICENSE` と `assets/fonts/` のライセンス文書を参照してください。
