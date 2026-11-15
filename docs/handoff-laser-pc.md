# レーザー用 PC への引き継ぎ

2026-11-16 時点のまとめ。Hazaism（端材教）の今の状態と、レーザー用 PC で次にやることを書く。
このリポジトリを clone すれば、ここから作業を続けられる。

## 1. 目的

いまの流れは、機械が 3 台で、受け渡しが 2 回ある。

```
スマホで撮影 ──写真──▶ PC で敷き詰め・DXF ──DXF──▶ レーザー用 PC（SmartDIYs Cutter）で切断
```

目標は、**レーザー用 PC 1 台で、撮影から切断まで済ませる**こと。

```
レーザー用 PC: Web カメラで撮影 → Hazaism で敷き詰め → SmartDIYs Cutter で切断
```

## 2. いまのアプリ

| 項目 | 内容 |
|---|---|
| 公開ページ | https://aho-wono.github.io/hazaism/ （v0.4.0） |
| リポジトリ | https://github.com/Aho-Wono/hazaism （Public、MIT） |
| しくみ | ブラウザだけで動く。計算は Pyodide（ブラウザの中で動く Python、v314.0.8）。サーバーはなく、GitHub Pages が静的なファイルを配るだけ |
| 公開の手順 | main に push すると、GitHub Actions が検査 → 組み立て → 公開（`.github/workflows/pages.yml`）。数分で反映 |
| データ | 写真・図面は外へ出ない。作業はブラウザの IndexedDB に保存（ブラウザやパソコンをまたいでは引き継がれない） |
| 敷き詰めの初期値 | 間隔 0.5 mm・余白 1.5 mm・回転の刻み 2°・計算時間 10 秒（一度変えた値はブラウザが覚える） |

主なファイル:

| 場所 | 中身 |
|---|---|
| `hazaism/vision/` | 写真 → 輪郭（格子の検出、真上から見た画像への補正、板の切り出し、直線＋円弧への当てはめ） |
| `hazaism/nest/` | 敷き詰め（`raster.py` 置ける位置の計算、`engine.py` 探索と作業スレッド用の `NestWorker`、`verify.py` 検査と実測） |
| `hazaism/dxfio/` | 部材 DXF の読込、結果の書き出し（端材の輪郭は青の線） |
| `hazaism/jobs.py`・`webapi.py` | 作業の状態と、画面からの頼みごとの入口 |
| `hazaism/static/js/app.js` | 画面の流れと写真の読み込み（`uploadPhoto`） |
| `hazaism/static/js/backend.js` | 画面とブラウザ内の Python との橋渡し、敷き詰めの作業スレッド（最大 4 本）の指揮 |
| `hazaism/static/js/py-core-worker.js`・`py-nest-worker.js` | Python を動かす作業スレッド |
| `tools/build_site.py` | 公開するサイトを `site/` に組み立てる |

### SmartDIYs Cutter で使うとき

SmartDIYs Cutter は DXF のレイヤーではなく**線の色ごと**にレイヤーを分ける（公式マニュアル 4.2 節）。
Hazaism は端材の輪郭を青（AutoCAD の色番号 5）で書き出すので、読み込んだあと「レイヤーパラメータ」で
青のレイヤーを**「加工なし」**にすれば、部材だけが切られる。切る前に「経路確認」（レーザーを出さずに経路どおり動く）で確かめる。

### 速さ（ノート PC の Chromium で計測）

| 段階 | 電源につないだとき | バッテリーのとき |
|---|---|---|
| 起動（Python の読み込み） | 16〜20 秒 | 25〜33 秒 |
| 写真の解析 | 12 秒 | 27〜37 秒 |
| 敷き詰め（計算 10 秒の設定） | 18〜28 秒（刻み 5°） | 28〜43 秒（43 秒は刻み 2°） |

作業スレッド 1 本で約 350 MB、計算中は全体で 1〜2 GB のメモリを使う。

## 3. 改善案

| 案 | 受け渡し | 開発 | 内容 |
|---|---|---|---|
| ① レーザー用 PC で Hazaism を開く | 2 回 → 1 回 | 不要 | 敷き詰めた DXF をその場で SmartDIYs Cutter に読み込める |
| ② Web カメラで撮る機能を足す | 1 回 → 0 回 | 小さめ | レーザー用 PC に USB の Web カメラをつなぎ、マットの上に固定。「カメラで撮る」ボタンから解析まで進む |
| ③ DXF を Cutter で直接開く | 手間が減る | 不要 | Windows で .dxf を SmartDIYs Cutter に関連付け、ブラウザのダウンロード表示から開く（要確認） |
| ④ PC アプリとしてインストール | ― | 小さい | Chrome/Edge の「アプリとしてインストール」で、デスクトップのアイコンから専用の窓で開けるようにする |
| ⑤ 加工機の中にカメラを固定 | 原点合わせも不要 | 大きい | 板を加工機に置いたまま真上から撮り、機械の座標のまま DXF を出す |

**進める順番**: ①を試す → ②と④を作る → ③を確かめる。⑤は①〜④を使ってみて、まだ面倒なら考える。

## 4. レーザー用 PC で最初に確かめること

- [ ] **メモリの量**（設定 → システム → バージョン情報）。SmartDIYs Cutter の推奨は 4 GB 以上。
      4 GB だと Hazaism は作業スレッドを自動で 2 本に減らすが、Cutter と同時に使うと重いかもしれない
- [ ] **ネット接続**。初回の読み込みだけ要る（2 回目からはオフラインでも開ける）
- [ ] **ブラウザ**。Chrome か Edge（DXF の保存先を選べる）
- [ ] **①を一通り試す**: Hazaism を開く → 写真を読み込む → 敷き詰め → DXF 保存 → Cutter で読み込み → 青を「加工なし」→ 経路確認。各段の時間を記録する
- [ ] **Web カメラ**。持っているか、作業台のどこに付けられるか（マット全体が真上から入る高さ）
- [ ] **③**: .dxf を SmartDIYs Cutter に関連付けて、ダブルクリックで開けるか。
      Cutter の「ユーザー設定 → ファイルインポート設定」にある「外部呼び出し時に直接加工出力」が何をするか

## 5. ②「カメラで撮る」の作り方メモ

- 写真の画面（`hazaism/static/index.html` の 1. 写真、`app.js`）に「カメラで撮る」ボタンとカメラの選択を足す
- カメラを開く: `navigator.mediaDevices.getUserMedia({ video: { deviceId, width: { ideal: 3840 }, height: { ideal: 2160 } } })`。
  カメラの一覧は `enumerateDevices()`。選んだカメラと解像度はブラウザに覚えさせる
- 撮る: Chromium では `ImageCapture.takePhoto()` で最大解像度の静止画を取れる。使えないときは映像の 1 コマを canvas に描いて JPEG にする
- 撮った画像は今の `uploadPhoto(file)` にそのまま渡す。解析はスマホの写真と同じ流れ
- ピントと露出は、固定できるカメラなら固定する（`applyConstraints` の focusMode / exposureMode。対応はカメラによる）
- 4K のカメラなら、幅 50 cm を 1 画素 0.13 mm ほどで写せる（いまのスマホ写真は 0.21 mm）。
  Web カメラはレンズのゆがみが大きめだが、格子の当てはめでゆがみも補正している。実際のカメラで確かめること
- カメラは https か http://127.0.0.1 のページでしか使えない。公開ページ（https）も手元の開発（127.0.0.1）も条件を満たす
- 自動の検査: Chromium は `--use-fake-device-for-media-stream --use-file-for-fake-video-capture=<動画ファイル>` で
  偽のカメラ映像を流せる見込み。テスト写真を動画にして流せば、Playwright で通しの検査ができる

## 6. ④ PC アプリとしてインストール

いまは manifest が無いので、Chrome/Edge の「アプリとしてインストール」が出ない。
`hazaism/static/manifest.webmanifest`（名前・アイコン・`display: standalone`）とアイコン画像を足し、`index.html` から読む。
service worker（`sw.js`）はすでにあるので、それだけでインストールできるようになるはず。

## 7. 開発の始め方（レーザー用 PC で手を入れる場合）

```
git clone https://github.com/Aho-Wono/hazaism.git
cd hazaism
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # uv が無ければ入れる
uv sync
uv run python -m hazaism              # サイトを組み立てて http://127.0.0.1:8765/ で開く
uv run pytest                         # 検査（約 3〜7 分）
uv run playwright install chromium    # 画面の通しの検査（tests/test_e2e.py）に必要。ネット接続も要る
```

- 実物の写真を使う検査は `tests/data/scrap_sample_01.png` を置いたときだけ走る。この写真はリポジトリに入れていない
  （作業場の写真なので）。必要なら元の PC から USB メモリなどでコピーする
- コミットの作者は、個人のメールアドレスを公開しないよう GitHub の noreply アドレスにしている:
  ```
  git config user.name "Aho-Wono"
  git config user.email "121322339+Aho-Wono@users.noreply.github.com"
  ```
- push には GitHub へのログインが要る（初回の push でログインの窓が出る）
- 版を上げるときは `pyproject.toml` の `version`。Pyodide の版は `hazaism/static/js/pyconfig.js`

## 8. これまでに分かった注意点

- **Pyodide**: JavaScript の null は Python で None にならない（JsNull になる）ので、無い値は undefined で渡す。
  threading と multiprocessing は使えない（並列は作業スレッドを複数立てて行う）
- **大きな円での `cv2.dilate` / `cv2.erode` はブラウザ内で極端に遅い**（板の切り出しが 82 秒かかった）。距離変換で同じことをする
- **GrabCut は OpenCV 共通の乱数を使う**ので、`cv2.setRNGSeed` で種を固定している（固定しないと実行順で結果が変わる）
- **図の上の文字**は `#layer-labels`（画面の座標の層）に描く。図の中に置くと、拡大したときにふちどりが太って黒くつぶれる
- **GitHub Actions**: `astral-sh/setup-uv` には v10 のような短い版名が無いので、正確な版（v10.3.3）で指定する
- **GitHub API に日本語の説明文を送るとき**は、UTF-8 のファイルにして `--data-binary` で送る（Git Bash の curl に直接書くと JSON の読み取りに失敗する）
- **計算が遅いとき**は、まず電源につながっているかを見る（バッテリー運転だと 2〜3 倍遅くなった）
