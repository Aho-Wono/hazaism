# レーザー用 PC への引き継ぎ

2026-11-16 時点のまとめ。Hazaism（端材教）の今の状態と、レーザー用 PC で次にやることを書く。
このリポジトリを clone すれば、ここから作業を続けられる。

## 1. 目的

以前の流れは、機械が 3 台で、受け渡しが 2 回あった。

```
スマホで撮影 ──写真──▶ PC で敷き詰め・DXF ──DXF──▶ レーザー用 PC（SmartDIYs Cutter）で切断
```

目標は、**撮ったらすぐレーザー用 PC で敷き詰め・切断まで進める**こと。0.5.0 で「スマホで撮って送る」を入れたので、
レーザー用 PC で Hazaism を開いておけば、次の流れになる（受け渡しの手作業なし）。

```
スマホで「撮って送る」 ──WebRTC で直接──▶ レーザー用 PC: Hazaism で解析・敷き詰め → SmartDIYs Cutter で切断
```

Web カメラや加工機の中のカメラで直接撮る案は、検討したがやめた（2026-11-16）。

## 2. いまのアプリ

| 項目 | 内容 |
|---|---|
| 公開ページ | https://aho-wono.github.io/hazaism/ （v0.6.0） |
| スマホの送信ページ | https://aho-wono.github.io/hazaism/send.html （PC の Hazaism の「スマホ」で出る QR から開く） |
| リポジトリ | https://github.com/Aho-Wono/hazaism （Public、MIT） |
| しくみ | ブラウザだけで動く。写真の解析・図形・DXF は Pyodide（ブラウザの中で動く Python、v314.0.8）、敷き詰めの探索は JavaScript（0.6.0 から）。サーバーはなく、GitHub Pages が静的なファイルを配るだけ |
| スマホ → PC | WebRTC で直接つなぐ。互いを見つける仲介だけ PeerJS の公開サーバー（0.peerjs.com）。写真は暗号化されて直接流れる |
| 公開の手順 | main に push すると、GitHub Actions が検査 → 組み立て → 公開（`.github/workflows/pages.yml`）。数分で反映 |
| データ | 写真・図面は外へ出ない。作業はブラウザの IndexedDB に保存（ブラウザやパソコンをまたいでは引き継がれない） |
| 敷き詰めの初期値 | 間隔 0.5 mm・余白 1.5 mm・回転の刻み 2°・計算時間 10 秒（一度変えた値はブラウザが覚える） |

主なファイル:

| 場所 | 中身 |
|---|---|
| `hazaism/vision/` | 写真 → 輪郭（格子の検出、真上から見た画像への補正、板の切り出し、直線＋円弧への当てはめ） |
| `hazaism/nest/` | 敷き詰めの準備（`engine.py` 向きの一覧・格子の画像・JavaScript に渡すデータ、`raster.py` 格子化）と、`verify.py` 検査と実測 |
| `hazaism/dxfio/` | 部材 DXF の読込、結果の書き出し（端材の輪郭は青の線） |
| `hazaism/jobs.py`・`webapi.py` | 作業の状態と、画面からの頼みごとの入口 |
| `hazaism/static/js/app.js` | 画面の流れ、写真の読み込み（`uploadPhoto`）、スマホから届いた写真の扱い（`receivePhoto`） |
| `hazaism/static/js/backend.js` | 画面とブラウザ内の Python との橋渡し、敷き詰めの作業スレッド（最大 8 本）の指揮 |
| `hazaism/static/js/py-core-worker.js` | Python を動かす作業スレッド |
| `hazaism/static/js/nest/engine.js`・`nest-worker.js` | 敷き詰めの探索（JavaScript）と、それを回す作業スレッド |
| `hazaism/static/js/phone-link.js` | スマホ → PC の送り方の取り決め（両方で使う） |
| `hazaism/static/js/phone-receive.js` | PC 側の待ち受け、QR の画面、上の帯の「スマホ」ボタン |
| `hazaism/static/send.html`・`js/send.js`・`css/send.css` | スマホの送信ページ（Python は読まない） |
| `hazaism/static/js/vendor/` | 同梱したライブラリ（PeerJS 1.5.5、qrcode-generator 2.0.4。出典とライセンスは `LICENSES.txt`） |
| `tools/build_site.py` | 公開するサイトを `site/` に組み立てる |
| `tools/make_icons.py` | アイコン（`hazaism/static/icons/`）を作る |

### SmartDIYs Cutter で使うとき

SmartDIYs Cutter は DXF のレイヤーではなく**線の色ごと**にレイヤーを分ける（公式マニュアル 4.2 節）。
Hazaism は端材の輪郭を青（AutoCAD の色番号 5）で書き出すので、読み込んだあと「レイヤーパラメータ」で
青のレイヤーを**「加工なし」**にすれば、部材だけが切られる。切る前に「経路確認」（レーザーを出さずに経路どおり動く）で確かめる。

### 速さ（ノート PC の Chromium で計測。電源につないだとき）

| 段階 | 時間 |
|---|---|
| 起動（Python の読み込み） | 10〜20 秒（バッテリーだと 25〜33 秒） |
| 写真の解析 | 11〜12 秒（バッテリーだと 27〜37 秒） |
| 敷き詰め（計算 1 秒 / 10 秒の設定。準備と確かめを含む全体） | 3.0 秒 / 12.0 秒（0.5 までは 10 秒の設定で 18〜28 秒）。作業スレッド 8 本で 1 秒あたり約 29 回（0.5 までは 4 本で 0.8 回） |
| スマホ → PC（つながるまで / 写真 1 枚） | 約 2 秒 / 1 秒前後（同じ PC 内で計測） |

敷き詰めの探索の比較（試験の部材 11 個・約 200 cm² の端材・刻み 2°・作業スレッド 4 本。どちらもこの PC の CPU で直接）:

| | 0.5 まで（Python） | 0.6（JavaScript） |
|---|---|---|
| 準備 | 1.3〜4 秒 | 0.6 秒（2 回目からは 0.06 秒） |
| 計算 1 秒 | 4 回・8 個（1 回に数秒かかり、1 秒では終わらない） | 34 回・7 個・58.9 % |
| 計算 10 秒 | 4 回・8 個・60.3 % | 41 回・9 個・62.3 % |

ブラウザの中の Python はこれよりさらに 3 倍ほど遅かったので、画面で使ったときの差はもっと大きい。
敷き詰めの作業スレッドは 1 本数十 MB（0.5 までは Python を読み込むので 1 本 350 MB、起動に約 10 秒）。

## 3. スマホで撮って送る（0.5.0）

使い方:

1. 初回だけ: PC の Hazaism で「スマホ」→「使い始める」→ 出た QR をスマホのカメラで読む → 開いたページをホーム画面に追加
2. 以後: スマホでアイコンを開く →「撮って送る」→ 撮る →「写真を使用」。PC で解析が始まり、結果がスマホにも出る

決めたこと:

- 仲介は PeerJS の公開サーバー（無料・準備不要）。止まっていると使えない（そのときは写真を移して読み込む）。
  困るようなら、自前の仲介サーバー（Cloudflare など）に替えられる（`new Peer(id, { host, port, path, key })`）
- PC で作業の途中（輪郭を手で直した後、敷き詰めた結果を DXF に保存する前、計算中）に届いたら、PC の画面で
  「読み込みますか？」と確かめる。それ以外（写真の画面、輪郭が検出結果のまま、DXF 保存後）は自動で読み込む
- PC の待ち受けの番号は `hazaism-` ＋ 乱数 24 文字（localStorage の `hazaism.phone`）。QR の URL は `send.html#pc=<番号>`
  （`#` 以降はサーバーに送られない）。スマホは URL の `#pc=` と localStorage（`hazaism.send`）の両方に覚える。
  iPhone のホーム画面のアプリは Safari と保存領域が別なので、URL の `#pc=` を消さずに残してある
- 手元の配信（127.0.0.1）で開いたときは、QR は公開版の send.html を指す（スマホから 127.0.0.1 は開けないため）

送り方（`phone-link.js`）: PeerJS の serialization は `"raw"`。合図は JSON の文字列
（`{t:"photo"}` → 16 KiB ずつの中身 → `{t:"end"}`）、PC は 1 MB ごとに `{t:"ack"}` を返し、スマホは返事を待ちながら送る
（4 MB 先まで）。PC は先頭のバイトで画像か確かめ、解析の段階を `{t:"status", stage}` で返す
（received / confirm / analyzing / done / declined / error）。

## 4. レーザー用 PC で最初に確かめること

- [ ] **メモリの量**（設定 → システム → バージョン情報）。SmartDIYs Cutter の推奨は 4 GB 以上。
      4 GB だと Hazaism は作業スレッドを自動で 2 本に減らすが、Cutter と同時に使うと重いかもしれない
- [ ] **ネット接続**。初回の読み込みと、スマホから送るとき（仲介のサーバー）に要る
- [ ] **ブラウザ**。Chrome か Edge（DXF の保存先を選べる）
- [ ] **スマホから送る**: Hazaism を開く →「スマホ」→「使い始める」→ QR を読む → 撮って送る。
      iPhone と Android の両方で、ホーム画面に追加して開き直しても送れるか。上の帯の「スマホ」の点が緑になるか
- [ ] **スマホとレーザー用 PC が同じ Wi-Fi か**。違うネットワークでも PeerJS の中継でつながるはずだが、遅くなる
- [ ] **Chrome の省メモリ**: 設定 → パフォーマンス →「常にアクティブにするサイト」に `aho-wono.github.io` を追加。
      Cutter を前に出している間に、Hazaism のタブが眠って受け取れなくならないか
- [ ] **一通り試す**: 撮って送る → 輪郭の確認 → 部材 → 敷き詰め → DXF 保存 → Cutter で読み込み → 青を「加工なし」→ 経路確認。
      各段の時間を記録する
- [ ] **DXF を Cutter で直接開けるか**: .dxf を SmartDIYs Cutter に関連付けて、ブラウザのダウンロード表示から開けるか。
      Cutter の「ユーザー設定 → ファイルインポート設定」にある「外部呼び出し時に直接加工出力」が何をするか

## 5. 残っている改善案

| 案 | 開発 | 内容 |
|---|---|---|
| 敷き詰めをさらに速く | 中くらい | 計算の中心は「置けない位置の形」（向きの組ごと、行×行の区間の足し算）。1〜5 秒の設定では格子を粗くしている（`engine.py` の `target_pixels`）。さらに速くするなら Rust → WebAssembly |
| PC アプリとしてインストール | 小さい | `index.html` 用の manifest（名前・アイコン・`display: standalone`）を足すと、Chrome/Edge の「アプリとしてインストール」が出る。アイコンは `hazaism/static/icons/` にある。sw.js はすでにある |
| DXF を Cutter で直接開く | 不要 | 4 節の最後の項目を確かめてから |
| 自前の仲介サーバー | 中くらい | PeerJS の公開サーバーが不安定なとき。PeerJS Server を Cloudflare などに置き、`new Peer()` の引数で指す |

## 6. 開発の始め方（レーザー用 PC で手を入れる場合）

```
git clone https://github.com/Aho-Wono/hazaism.git
cd hazaism
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # uv が無ければ入れる
uv sync
uv run python -m hazaism              # サイトを組み立てて http://127.0.0.1:8765/ で開く
uv run pytest                         # 検査（約 3〜7 分）
uv run playwright install chromium    # 画面の通しの検査（tests/test_e2e.py）に必要。ネット接続も要る
uv run playwright install webkit      # スマホの送信ページを Safari と同じしくみで開く検査（無ければ飛ばす）
```

- 実物の写真を使う検査は `tests/data/scrap_sample_01.png` を置いたときだけ走る。この写真はリポジトリに入れていない
  （作業場の写真なので）。必要なら元の PC から USB メモリなどでコピーする
- スマホから送る検査（`test_7b_phone_send`）は、PeerJS の公開サーバーにつながらないと飛ばす
- コミットの作者は、個人のメールアドレスを公開しないよう GitHub の noreply アドレスにしている:
  ```
  git config user.name "Aho-Wono"
  git config user.email "121322339+Aho-Wono@users.noreply.github.com"
  ```
- push には GitHub へのログインが要る（初回の push でログインの窓が出る）
- 版を上げるときは `pyproject.toml` の `version`（`uv lock` で uv.lock も合わせる）。Pyodide の版は `hazaism/static/js/pyconfig.js`

## 7. これまでに分かった注意点

- **Pyodide**: JavaScript の null は Python で None にならない（JsNull になる）ので、無い値は undefined で渡す。
  threading と multiprocessing は使えない（並列は作業スレッドを複数立てて行う）
- **大きな円での `cv2.dilate` / `cv2.erode` はブラウザ内で極端に遅い**（板の切り出しが 82 秒かかった）。距離変換で同じことをする
- **GrabCut は OpenCV 共通の乱数を使う**ので、`cv2.setRNGSeed` で種を固定している（固定しないと実行順で結果が変わる）
- **図の上の文字**は `#layer-labels`（画面の座標の層）に描く。図の中に置くと、拡大したときにふちどりが太って黒くつぶれる
- **敷き詰めの探索（engine.js）**: 位置 (x, y) は部材のマスクの左上の画素。「置けない位置」B は行ごとに 32 ビットずつ詰めた整数。
  端材に対する分は「部材の各行の区間が空き区間に収まる x の範囲」、置いた部材に対する分は区間どうしの足し算で求め、
  後者は向きの組ごとに覚えて使い回す（`nfrShape`）。試す向きは部材ごとの組を 4 分の 1 ずつ入れ替える（毎回ばらばらに選ぶと、
  覚えた形が使えず 5 倍ほど遅くなった）。正しさは `tests/test_nest_js.py` で 1 画素ずつ調べた答えと比べている
- **Python → JavaScript のデータ**: `engine.prepare()` が "HZN1" ＋ 見出しの JSON ＋ ビットに詰めた画像を返す。
  部材は反転ごとに 1 回だけ太らせて（buffer）から回す（向きごとに buffer するのと画素まで同じになることを確かめた）
- **WebRTC の検査**: 同じ PC の中で 2 つのページをつなぐときは、Chromium を
  `--disable-features=WebRtcHideLocalIpsWithMdns` で起動する（自分の IP を mDNS の名前で隠すと、つながらないことがある）
- **PeerJS**: スマホ側は、仲介のサーバーと切れても `peer.destroy()` しない（つながっている PC とのやり取りまで切れる）。
  PC 側は `disconnected` で `reconnect()`、それ以外の失敗は作り直して間隔を空けてやり直す
- **GitHub Actions**: `astral-sh/setup-uv` には v10 のような短い版名が無いので、正確な版（v10.3.3）で指定する
- **GitHub API に日本語の説明文を送るとき**は、UTF-8 のファイルにして `--data-binary` で送る（Git Bash の curl に直接書くと JSON の読み取りに失敗する）
- **計算が遅いとき**は、まず電源につながっているかを見る（バッテリー運転だと 2〜3 倍遅くなった）
