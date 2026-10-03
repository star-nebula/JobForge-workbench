# JobForge Workbench

**Language / 语言 / 言語 / Idioma / 언어 / Langues / Sprachen:** [English](../README.md) | [简体中文](README_zh-CN.md) | [日本語](README_ja.md) | [한국어](README_ko.md) | [Français](README_fr.md) | [Deutsch](README_de.md) | [Español](README_es.md)

ローカルで完結する求人活動ワークベンチ：履歴書 → キーワード → 求人スクレイピング（BOSS直聘）→ マッチ度ランキングを一気通貫で行う単体ツールです。

> **プラットフォーム要件：Windows 10/11 のみ対応。** ネイティブスクレイピングは Windows UI オートメーション（pywinauto / UIA TextPattern）とキーボード・マウス操作の自動化（pyautogui / pygetwindow）に依存しており、これらの Windows 専用ライブラリはサーバー起動時の import で読み込まれるため、macOS/Linux ではサーバー自体が起動しません。デスクトップ版 Chrome と Python 3.10+ も必要です。

![ダッシュボード](../docs/screenshots/dashboard.png)

## 機能

- **プロフィール**：履歴書のアップロードと解析、フィールド単位の比較・取り込み、ローカルルールによるスコアリングと改善提案、履歴書プレビュー、PDF エクスポート
- **スマートスクレイピング**：履歴書のキーワードから BOSS の求人を取得（ネイティブブラウザ経路）。取得履歴と求人単位の重複防止付き
- **求人マーケット**：求人の DB 登録、JD 詳細の取得と本文クリーニング、マッチ度ソート、JD 取得統計（取得済み / 未取得 / 不完全の疑い。ピルをクリックしてフィルタ可能。不完全の疑いがあるものは詳細モーダルで手動確認または再取得できます）。マッチ度は二段構え：**タグ事前スクリーニング**（ローカルルールの 4 軸：スキル / 志向 / 給与 / 都市。スキル分母は求人側のスキルタグ、語の正規化後の完全一致、給与は K 単位に正規化、保証なしの実質 0-100）はスクレイピング時に計算、**JD 精密マッチング**（LLM が JD 全文＋タグ / 給与 / 都市のハード情報を読解）は詳細モーダルまたは一括分析で生成。カードには青い「AI xx」バッジが付きます
- **AI 機能**（マルチモデル設定、OpenAI 互換プロトコル、キーはローカル SQLite のみに保存）：BOSS の挨拶文生成、求人 AI マッチ分析（詳細モーダルで自動実行＋求人マーケットで一括実行）、履歴書のリファイン（diff 比較後に取り込み）
- **応募パイプライン**：カンバンのドラッグ＆ドロップで 6 ステータスを管理（discovered / reviewing / applied / interviewing / rejected / offered）
- **面接スケジュール**：「面接中」の求人から面接日時とメモを設定
- **メッセージセンター**：BOSS直聘の会話を読み取り専用で同期（CDP レスポンス傍受）
- **フローティング進捗ウィンドウ**：スクレイピング中は最前面で進捗を表示し、一時停止 / 再開 / 終了ボタン付き。ウィンドウはフォーカスを奪わずマウスクリックも透過するため、**進行中のキーボード・マウスによるスクレイピングを妨げません**。⠿ ハンドルをドラッグして移動できます

## ディレクトリ構成

```
JobForge-workbench/
├─ src/jobforge/                 # コード：Python パッケージ
│  ├─ server.py                  # FastAPI エントリポイント
│  ├─ paths.py                   # プロジェクトパスの唯一のソース（コード位置とデータ位置を分離）
│  ├─ spider.py  fetch_jd.py  fetch_jd_native.py  fetch_gate.py
│  ├─ db.py  llm.py  profile_score.py
│  └─ tools/                     # サブプロセススクリプト。server が `python -m jobforge.tools.*` で起動
│     └─ hud.py  messages.py  grab_cookies.py
├─ web/job-workbench.html        # フロントエンド単一ページ（6 ビュー）
├─ data/                         # 実行時データ（コミット対象外）：jobs.db、cookies.json、messages.json、ゲート/スロットルファイル、chrome-profile/
├─ tests/                        # pytest 回帰テスト（先に `pip install -r requirements-dev.txt` を実行してから `venv\Scripts\python.exe -m pytest tests/`。件数はドキュメントに書かず実行で確認）
├─ run.bat  setup.bat  requirements.txt  README.md
```

データファイルのパスはすべて `paths.py` から取得し、各モジュールが `__file__` から推測することはありません。コードを移動してもデータは付きません。
CDP デバッグ用 Chrome の user-data-dir（BOSS のログイン状態を含む）もデータディレクトリ配下：`data/chrome-profile/`。

応募ステータスの語彙は一つだけ：フロントエンドの `STATUS_META`（カンバン列、詳細モーダルのステータス選択、ダッシュボードのパイプラインはすべてここから派生）と、
バックエンドの `db.VALID_STATUSES`。両者は同集合で、各ステータスには到達可能な書き込み経路があり、
`tests/test_frontend_status_contract.py` で固定されています。かつては 6 ステータスを宣言しながらカンバンは 4 列しか描画せず、
`rejected`/`offered` は UI から設定できず、DB 内の 114 件の求人は 2 値しか残っていませんでした。

注意：`.bat` は CRLF 行末を維持する必要があります（`.gitattributes` で `*.bat text eol=crlf` を宣言済み）。「`chcp` でのコードページ切替 + 中国語コメント + 裸 LF」の条件下では cmd.exe がバイトオフセットでずれて解析し、`set "PYTHONPATH=..."` の行頭を黙って食い潰します。症状は起動時の `ModuleNotFoundError: No module named 'jobforge'` です。

手動起動（IDE / コマンドライン）では `src` が `PYTHONPATH` に必要です。なければ `import jobforge` が失敗します：

```
set PYTHONPATH=%CD%\src
venv\Scripts\python.exe -m jobforge.server
```

## アーキテクチャ

| ファイル | 役割 |
|---|---|
| `src/jobforge/server.py` | FastAPI エントリポイント、`127.0.0.1:8080`（`--port` で変更可。2 インスタンス目のデバッグ用） |
| `src/jobforge/paths.py` | プロジェクトパスの唯一のソース：`PROJECT_ROOT` / `DATA_DIR` / `WEB_DIR` |
| `src/jobforge/spider.py` / `fetch_jd_native.py` | 求人リストと JD 詳細のスクレイピング（ネイティブキーボード/マウス + UIA TextPattern 経路） |
| `src/jobforge/fetch_jd.py` | JD 取得の総入口（native 優先、CDP フォールバック）+ JD 本文クリーニング |
| `src/jobforge/tools/hud.py` | フローティング進捗ウィンドウ（独立プロセス。最前面 + フォーカス非奪取 + クリックスルーの 3 制約はファイル先頭のコメント参照） |
| `src/jobforge/fetch_gate.py` | プロセス横断の一時停止/停止ゲート：シグナルは `data/fetch_gate.json` に書かれ、server スレッドとスクレイピングサブプロセスで共有 |
| `src/jobforge/tools/messages.py` | BOSS メッセージ同期（Playwright CDP で 9222 ポートのブラウザに接続しページレスポンスを傍受） |
| `src/jobforge/db.py` | SQLite（WAL）：求人 / メッセージ / プロフィール / 取得履歴 |
| `src/jobforge/profile_score.py` | プロフィールのローカルルールスコアリングエンジン（13 項目のチェック） |
| `src/jobforge/llm.py` | LLM 機能層（OpenAI 互換 chat クライアント + 挨拶文 / マッチ分析 / 履歴書リファインの 3 関数） |
| `web/job-workbench.html` | フロントエンド単一ページ（6 ビュー） |
| `src/jobforge/tools/grab_cookies.py` | ブラウザのログイン Cookie を取得して `data/cookies.json` に書き込み |

## 使い方

1. `setup.bat` をダブルクリックして venv を作成し依存関係をインストール
2. `run.bat` をダブルクリックして起動し、ブラウザで <http://127.0.0.1:8080> を開く
3. デスクトップ Chrome で zhipin.com にログインするとスクレイピング可能（スクレイピング中は約 8〜15 秒キーボードとマウスを占有します。メッセージ更新には 9222 デバッグポート付きのブラウザが必要）
4. 右上の ⚙ で AI モデルを設定すると、挨拶文生成 / マッチ分析 / 履歴書リファインが使えます（DeepSeek / 通義 / 智譜 / Ollama など OpenAI 互換サービス全般に対応）
5. 一括 AI 分析の前提条件：デスクトップ Chrome が zhipin.com にログイン済みで開いていること（ウィンドウは最小化しない）。分析はキーボードとマウスを占有し、3 連続失敗で自動的にサーキットブレーカが作動して停止します。server には単一インスタンスガードがあり、重複起動は拒否されます
6. スクレイピングの進捗はフローティングウィンドウで確認（上部バーの「🪟 進捗ウィンドウ」で手動オープン。スクレイピング開始時にも自動で立ち上がります）：
   - **一時停止**は安全ポイント（求人の境界、スロットル待ち）でのみ止まり、キーボード/マウス操作の途中で分割することはありません。停止時間はスロットルにカウントされないため、再開後に待ち直す必要はありません
   - **終了**は数秒で効きます（実行中のスクレイピングサブプロセスも kill）。取得済みの求人と完了済みの AI 分析は保持されます
   - ウィンドウは既定で画面右下。⠿ をドラッグして移動。タスク終了後は数秒間結果を表示してから自動的に閉じ、✕ ですぐ閉じられます

## プライバシー

`data/`（`jobs.db`、`messages.json`、`cookies.json`、`fetch_gate.json`、`hud_pos.json` など）とブラウザプロファイル（`chrome-profile/`、正式位置は `data/chrome-profile`）は `.gitignore` で除外され、コミットされません。
