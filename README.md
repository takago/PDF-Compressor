# PDF Compressor

PDFファイルをドラッグ＆ドロップするだけで，埋め込み画像を再圧縮し，ファイルサイズを小さくするWebアプリケーションです．**1 MB以内を目標**に画像の解像度とJPEG品質を段階的に調整します．画像以外のテキスト，リンク，フォームの値は可能な限り維持し，処理後に検証します．1 MBへの縮小や全PDFでの表示の完全一致を保証するものではありません．

**GitHub:** https://github.com/takago/PDF-Compressor

## 主な機能

- PDFをドロップすると自動的にアップロード・圧縮・ダウンロードします．操作ボタンは不要です．クリックによるファイル選択も可能です．
- 1件ならPDF（`元ファイル名_compressed.pdf`），複数件ならZIP（`compressed_pdfs.zip`）を返します．サイズが小さくならないPDFは元の内容・ファイル名を維持します．
- 複数PDFは同じFIFOキューに登録し，**最大2件を並列**で圧縮します．**待機キューの上限は300件**です．
- 圧縮前後のサイズ・削減率を表示します．詳細診断はデバッグ表示に切り替えられます．
- 元PDFは処理完了時に削除し，出力ファイルは原則として処理完了から5分後に期限切れとなります（複数PDFの場合はバッチ全体の完了を基準とします）．

## 動作環境

- Python 3.10以降を想定
- LinuxなどのPythonが動作する環境
- 必要なライブラリ：FastAPI，Uvicorn，PyMuPDF，Pillow，python-multipart（`requirements.txt` に記載）
- GPUは不要です．

## セットアップ

リポジトリを取得します．

```bash
git clone https://github.com/takago/PDF-Compressor.git
cd PDF-Compressor
```

仮想環境を作成して依存ライブラリをインストールします．

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Webサーバを起動します．**Uvicornのワーカーは必ず1つ**にしてください．これは圧縮処理の同時実行数とは別の設定です．

```bash
uvicorn app:app --host 0.0.0.0 --port 9353 --workers 1
```

サーバ自身からは `http://localhost:9353/`，別のPCからは `http://<サーバのIPアドレス>:9353/` にアクセスします．終了するにはターミナルで `Ctrl+C` を入力します．HTTP運用では通信が暗号化されないため，利用するネットワークとアクセス範囲を適切に制限してください．

## systemdユーザーサービスとして実行する（Linux）

ログインユーザーの権限でPDF Compressorを起動するためのユニットファイルを，`systemd/pdf-compressor.service` に同梱しています．root権限でのシステムサービス登録は必要ありません．

**標準のユニットファイルは，リポジトリを `~/PDF-Compressor` に配置した場合に動作します．** まだクローンしていない場合は，次のようにホームディレクトリで取得し，上記の「セットアップ」に従って `.venv` を作成してください．

```bash
cd ~
git clone https://github.com/takago/PDF-Compressor.git
cd ~/PDF-Compressor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

すでに別の場所へクローンしている場合は，`systemd/pdf-compressor.service` の `WorkingDirectory` と `ExecStart` を実際の絶対パスに変更してから登録してください．`ExecStart` は仮想環境のPythonを使います．

### サービスの登録・有効化

```bash
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp systemd/pdf-compressor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pdf-compressor.service
systemctl --user status pdf-compressor.service
```

`enable --now` は，現在のユーザーセッションで起動すると同時に，今後のユーザーsystemd起動時にもサービスを自動起動する設定です．起動後は `http://localhost:9353/` にアクセスできます．

### ログ確認・再起動・停止

```bash
journalctl --user -u pdf-compressor.service -f
systemctl --user restart pdf-compressor.service
systemctl --user stop pdf-compressor.service
systemctl --user disable --now pdf-compressor.service
```

`stop` は今回の実行を停止し，`disable --now` は自動起動も無効化します．ユニットファイルを編集した後は `systemctl --user daemon-reload` を実行してから再起動してください．

**ログアウト後やOS起動直後にも，ログインせずサービスを稼働させる場合**は，管理者権限でlingerを有効化する必要がある場合があります．例えば次のコマンドです（システムの運用規程に従ってください）．

```bash
sudo loginctl enable-linger "$USER"
```

このサービスは `0.0.0.0:9353` でHTTPを待ち受けます．アクセス可能なネットワークを必ず制限してください．`--workers 1` はUvicornのプロセス数であり，PDF圧縮はアプリ内部で最大2並列です．**手動起動したUvicornと同時に動かさないでください**（ポート競合・キューの分離を防ぐため）．また，ユーザーサービスはログインセッションやlingerの設定により稼働期間が変わります．

## 使い方

### PDFを1件圧縮する

1. ブラウザでPDF Compressorを開きます．
2. PDFを画面へドロップします（枠をクリックしてファイル選択も可能です）．
3. 処理が始まり，混雑時は待機状況が表示されます．
4. 完了すると圧縮前後のサイズと削減率が表示され，PDFのダウンロードが自動的に始まります．リンクから再ダウンロードすることもできます（保存期限内のみ）．

### PDFを複数まとめて圧縮する

1. 複数のPDFを一度に選択して，同じドロップ領域に投入します．
2. 全件が共通のFIFOキューに登録され，処理状況が表示されます．
3. 全件の処理が終わると，成功したPDFをまとめた `compressed_pdfs.zip` が自動的にダウンロードされます．

一部のPDFで失敗した場合は，成功したPDFのみZIPに含め，画面に失敗件数と理由を表示します．同名のPDFがあってもZIP内でファイル名が衝突しないように処理します．すべて失敗した場合はZIPを作成しません．ブラウザを開いたままにしてください．

### デバッグ表示

通常の利用画面は最小限の情報だけを表示します．画像検出数，圧縮件数，未対応画像などの詳細を確認したいときは，URLに `?debug=1` を付けます．

```text
http://localhost:9353/?debug=1
```

これは**画面表示の切り替え**であり，認証・アクセス制限ではありません．PDFの本文や画像データを診断目的で外部送信する機能ではありません．

## キューと処理制限

| 項目 | 初期設定 |
| --- | --- |
| Uvicornワーカー | 1 |
| 同時圧縮プロセス | 2件 |
| FIFO待機キュー | 最大300件（処理中2件は別枠） |
| 1回にドロップ可能なPDF | 最大100件 |
| 1ファイルのアップロード上限 | 100 MB |
| 一括アップロード合計 | 500 MB |
| PDF圧縮の処理時間上限 | 120秒／件 |
| 待機時間上限 | 1時間 |
| ダウンロード可能期間 | 完了から5分間（バッチは全件完了から5分間） |

キューが満杯の場合，受付を拒否します．キューはメモリ内で管理しているため，サーバ再起動時に未処理ジョブは復元されません．複数のUvicornワーカーや複数のサーバインスタンスを同じ構成で起動するとキューが独立し，2並列という制限を維持できません．

待機中の元PDFはディスク上に保持されるため，300件の利用を想定する場合はディスク容量を監視してください．残った一時ファイルは起動時と定期清掃で回収します．

## 設定

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `PDF_OPT_TMP` | `/tmp/pdf-compressor` | 一時ファイルの保存先 |
| `PDF_OPT_MAX_MB` | `100` | 1ファイルあたりのアップロード容量の上限（MB） |

例：一時ファイルの保存先を変更して起動します．

```bash
PDF_OPT_TMP=/var/tmp/pdf-compressor uvicorn app:app --host 0.0.0.0 --port 9353 --workers 1
```

`PDF_OPT_MAX_MB` を変更するときは，`static/index.html` 内のクライアント側の制限・表示も合わせて変更してください．その他のキュー・時間制限の値は `app.py` で定義されています．

## API

| API | 用途 |
| --- | --- |
| `POST /api/compress` | PDF 1件の受付（multipartのフィールド名：`file`） |
| `GET /api/status/{id}` | 単一ジョブの待機・実行・完了・失敗状況 |
| `GET /api/download/{id}` | 処理後PDFのダウンロード |
| `POST /api/batches` | 複数PDFの受付（multipartのフィールド名：`files`） |
| `GET /api/batches/{id}` | 一括ジョブの進捗と各ファイルの結果 |
| `GET /api/batches/{id}/download` | 処理後PDFをまとめたZIPのダウンロード |

アップロード受付時はHTTP 202とジョブID・状態確認用URLを返します．ブラウザは状態確認を繰り返し，完了後にダウンロードします．待機中は接続を長時間維持しません．

## テスト

```bash
python -m pip install pytest httpx
python -m pytest -q
```

## 注意事項

- **通信は暗号化されません．電話番号やパスワードなど機密性の高い情報を含むPDFの利用は避けてください．** 学内ネットワークであってもHTTP通信には傍受・改変の危険があります．
- フォーム値，テキスト，リンクを維持する設計ですが，特殊なPDFや閲覧ソフトでの表示一致は保証できません．重要な文書は変換結果を確認してください．
- 電子署名付き・暗号化・XFA形式のPDFは処理しません．透過マスクなどを含む特殊な画像は再圧縮されないことがあります．
- PDFの構造次第では1 MBを達成できない場合があります．画像を圧縮しても小さくならなければ，元PDFを返します．
- サーバ上の一時ファイルは期限に従って削除されますが，ブラウザにダウンロードされたファイルは削除されません．
- 不特定多数に公開する場合は，コンテナ等による隔離，リクエスト制限，ディスク容量監視，ネットワークのアクセス制御を別途検討してください．

---

© TAKAGO_LAB 2026  ·  [GitHub](https://github.com/takago/PDF-Compressor)
