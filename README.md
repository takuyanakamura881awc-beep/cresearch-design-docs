# 日本株 自動売買ツール

資金50万円で日本株を自動売買するツール。三菱UFJ eスマート証券の
kabuステーションAPI を通じて発注する。

> **現在のステータス: Phase 0（設計）完了。** 本リポジトリには設計ドキュメントと
> インターフェース定義のスケルトンのみが入っている。実装は Phase 1 以降。
>
> **Phase 1〜3（Stage A）は証券口座もAPIも不要で着手できる。**
> データ源は J-Quants 無料（メール登録のみ）と yfinance。詳細は
> [docs/09-data-sources.md](docs/09-data-sources.md)。

---

## 中核となる設計方針

**信用取引を使うが、建玉総額を現金残高（50万円）以内に制限する — レバレッジ1倍固定。**

リスクを現物並みに保ったまま、信用取引のメリットだけを取り込む：

| 得られるもの | 理由 |
|---|---|
| **同一銘柄の当日複数回転** | 現物の差金決済規制（1銘柄1日1回転が上限）を回避できる |
| **空売り** | 下落局面でも収益機会。相場環境への依存を減らせる |
| **手数料0円・金利0%・貸株料0%** | デイトレ信用の条件。実質コストはスリッページのみ |
| **オーバーナイトリスクなし** | 当日決済なので場が閉じている間の暴落に晒されない |

この不変条件は `src/autotrader/risk/leverage.py` が全発注の必須通過点として強制する。

---

## 主軸手法

**竹 — デイトレード ロング/ショート（当日決済）**

| | |
|---|---|
| 保有期間 | 数十分〜大引け前（当日決済必須） |
| ポジション | 同時3〜5銘柄、各10〜15万円、1日5〜15トレード |
| シグナル | オープニングレンジ・ブレイクアウト / VWAP乖離の平均回帰 |
| 期待成績 | 年利 20〜50% / 最大DD 15〜30% |
| 検証データ | Stage A は yfinance の5分足（過去60日分が即座に取れる） |

将来の拡張：**松**（スキャルピング）は竹が実弾で安定稼働した後に検討。
**梅**（日足スイング）は資金300万円以上で追加。

詳細は [docs/04-strategies.md](docs/04-strategies.md)。

---

## 工程

**2段階構成。Stage A は口座もAPIも不要。**

### Stage A — 口座なし・APIなし（今すぐ着手可能）

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | 設計ドキュメント | ✅ 完了 |
| 1 | データ基盤（J-Quants 日足 + yfinance 分足） | ✅ 完了 |
| 2 | ユニバース構築 + バックテストエンジン | 未着手 |
| 3 | 竹の実装 + ヒストリカル検証 | 未着手 |

**ゲート: ここの成績で、口座を開く価値があるかを判定する。**
見込みがなければ口座開設・API申込のコストを払わずに戦略を練り直す。

### Stage B — 口座あり・APIあり（**運用資金は入れない**）

| Phase | 内容 | 状態 |
|---|---|---|
| 4 | kabuステーションAPI + ペーパー基盤 + 安全装置16項目 | 未着手 |
| 5 | 3ヶ月ペーパートレード | 未着手 |
| 6 | 評価 → 実弾移行（少額から段階的に） | 未着手 |

> **口座開設と入金は別。** 口座開設自体は無料・入金不要で、kabuステーションAPI も無料。
> Stage B は「口座は開くが運用資金50万円は入れない」状態。

**実弾移行には事前定義した合格基準をすべて満たす必要がある**
（[docs/07-go-live-criteria.md](docs/07-go-live-criteria.md)）。基準は後から緩めない。

---

## ドキュメント

| ファイル | 内容 |
|---|---|
| [00-overview.md](docs/00-overview.md) | 全体像・工程・意思決定ログ |
| [01-broker-api.md](docs/01-broker-api.md) | kabuステーションAPI の仕様と制約 |
| [02-margin-rules.md](docs/02-margin-rules.md) | 信用取引ルール・レバ1倍の不変条件・当日決済要件 |
| [03-universe.md](docs/03-universe.md) | 銘柄選定の2層構造・プレミアム枠・バイアス回避 |
| [04-strategies.md](docs/04-strategies.md) | 竹（メイン）・松/梅（将来）の手法定義 |
| [05-risk-management.md](docs/05-risk-management.md) | リスク管理・安全装置16項目 |
| [06-operations.md](docs/06-operations.md) | 日々の運用手順・障害時対応・キルスイッチ |
| [07-go-live-criteria.md](docs/07-go-live-criteria.md) | 実弾移行の合格基準 |
| [08-development-harness.md](docs/08-development-harness.md) | 開発ハーネス（ロール分担とモデル選択） |
| [09-data-sources.md](docs/09-data-sources.md) | **データ源と Stage A/B の2段階構成** |

---

## 開発ハーネス

Claude Code で開発するためのロールを `.claude/agents/` に定義してある。
**誤りが金銭損失に直結する**ため、書く人と検証する人を構造的に分離している。

| ロール | モデル | 権限 | 責務 |
|---|---|---|---|
| `architect` | Opus 5 | 読み書き | 設計判断・インターフェース定義 |
| `implementer` | Sonnet 5 | 読み書き | 確定した仕様の実装 |
| `data-engineer` | Sonnet 5 | 読み書き | データ取得・整形・永続化 |
| `test-writer` | Sonnet 5 | 読み書き | テスト作成（仕様から書く） |
| `reviewer` | Opus 5 | 読み取りのみ | コードの正しさ検証 |
| `risk-auditor` | Opus 5 | 読み取りのみ | 安全装置16項目の監査 |
| `backtest-validator` | Opus 5 | 読み取りのみ | バイアス検出・過学習検証 |

**判断が要る仕事に Opus、手が要る仕事に Sonnet。**
検証系ロールは read-only にして、指摘の独立性を保っている。

詳細は [docs/08-development-harness.md](docs/08-development-harness.md)。

---

## セットアップ

### 前提となる環境

- **Python 3.11 以上**
- **Stage B のみ: Windows PC（常時起動）** — kabuステーションAPI は Windows 専用で、
  取引時間中はアプリの常駐が必要。**Stage A では不要**（OS を問わない）

### 事前に必要な手続き（人手）

**Stage A（Phase 1〜3）で必要なのはこれだけ:**

1. **J-Quants の無料登録** — メールアドレスのみ。**証券口座は不要**
2. **Freeプランへの登録** — ログイン後、プラン選択画面で `Get started`。
   **ユーザー登録しただけでは API が使えない**（ここでつまずきやすい）
3. ダッシュボード →「API Keys」でキーを発行

**Stage B（Phase 4〜）に入る時点で追加:**

4. 三菱UFJ eスマート証券の**口座開設 + 信用取引口座の開設**
5. **kabuステーションAPI の利用申込** — メンバーサイト →「設定・申込」→「らくらく電子契約」
   → 取引ツール欄の kabuステーションAPI設定（書面・捺印不要）
6. Windows PC の**スリープ無効化**と kabuステーションの自動起動設定

### セットアップ（Windows）

```powershell
mkdir C:\PrivateTule -Force
cd C:\PrivateTule
git clone https://github.com/takuyanakamura881awc-beep/cresearch-design-docs.git jp-autotrader
cd jp-autotrader
git checkout claude/japan-stock-trading-tool-6evhcd
```

**認証情報を設定する**（`<キー>` を発行した APIキーに置き換え）:

```powershell
Copy-Item .env.example .env
(Get-Content .env) -replace '^JQUANTS_API_KEY=.*', 'JQUANTS_API_KEY=<キー>' | Set-Content .env -Encoding UTF8
git status    # .env が出てこないこと（.gitignore 済み）
```

> メモ帳は使わないこと。拡張子が `.env.txt` になる。

**依存をインストールする:**

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

### データ源の実測（Phase 1 の最初にやること）

```powershell
python scripts\verify_data_sources.py
```

設計時の想定（yfinance は1分足7日/5分足60日、J-Quants Free は12週遅延）は
**公開仕様に基づく未検証の値**。実測して確認してから Phase 2 に進む。

このスクリプトが出力するもの:

| # | 内容 | 使いみち |
|---|---|---|
| 1 | J-Quants の疎通 | APIキーが有効か |
| 2 | yfinance の実際の取得可能期間 | 想定と違えば `MAX_LOOKBACK_DAYS` を修正 |
| 3 | J-Quants Free の実際のデータ終端日 | 12週遅延の実測 |
| 4 | 期間ズレ | 5分足と日足の間に何日の穴があるか |
| 5 | **日足の突合（J-Quants vs yfinance）** | **Light（1,650円/月）課金の判断材料** |
| 6 | yfinance のレート制限の挙動 | ブロックされていないか |

### 動作確認

```bash
python -c "import autotrader; print(autotrader.__version__)"
pytest
```

---

## セキュリティ

**認証情報は絶対にリポジトリに含めない。**
API パスワードと J-Quants の API キーは `.env`（`.gitignore` で除外済み）に置き、
環境変数経由でのみ読み込む。コミット前に `git status` で `.env` が
未追跡になっていることを確認すること。

---

## 免責

本ツールは自己資金の運用を目的とした個人プロジェクトであり、投資助言ではない。
自動売買には資金を失うリスクがあり、その責任は運用者本人が負う。
