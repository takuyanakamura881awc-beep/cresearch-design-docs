"""yfinance（Yahoo Finance）からの日足・分足の取得。**Stage A の分足の入手経路。**

**証券口座もAPIキーも不要。** ライブラリを入れるだけで使える。

【当初計画のボトルネックを解いた経路】
当初は「分足はどこからも取れないので kabuステーションAPI で3ヶ月かけて
自前蓄積するしかない」という制約があった。yfinance は**5分足を過去60日分
まとめて取得できる**ため、待たずに検証を始められる
（docs/09-data-sources.md §2.3）。

【取得可能な期間 — Yahoo Finance API の制限】

=========  ==============
足         遡れる期間
=========  ==============
1分足      **直近7日**
2〜90分足  **直近60日**
60分足     直近730日
日足       制限なし
=========  ==============

【注意】
- **非公式API。** 規約・可用性・品質の保証がない。落ちる前提で組む
- 遅延15〜20分。リアルタイムではない（Stage A は過去データ検証なので支障なし）
- **板情報（bid/ask/厚み）は取れない** → 約定モデルを保守的に倒す
- 日本株のティッカーは ``7203.T`` のようにサフィックス ``.T``
"""

from __future__ import annotations

from datetime import date

from autotrader.types import Bar

MAX_LOOKBACK_DAYS: dict[str, int] = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "90m": 60,
}
"""足ごとの遡れる日数。Yahoo Finance API の制限。

Phase 1 の最初に**実機で実測して確認する**こと。
サンドボックス環境ではネットワークポリシーにより検証できていない。
"""


def to_ticker(code: str) -> str:
    """銘柄コードを Yahoo のティッカーに変換する（``7203`` → ``7203.T``）。"""
    raise NotImplementedError("Phase 1 で実装する")


def fetch_bars(
    code: str,
    interval: str,
    start: date,
    end: date,
) -> tuple[Bar, ...]:
    """バーを取得する。

    Args:
        code: 銘柄コード（``.T`` なし）。
        interval: ``1m`` / ``5m`` / ``1d`` など。
        start, end: 取得期間。``MAX_LOOKBACK_DAYS`` を超える指定は
            **黙って切り詰めず、エラーにする**（取れたつもりで欠損するのを防ぐ）。

    Raises:
        ValueError: 期間が ``MAX_LOOKBACK_DAYS`` を超える場合。
    """
    raise NotImplementedError("Phase 1 で実装する")


def rolling_collect_1m(codes: tuple[str, ...], output_dir: object) -> None:
    """1分足を日次でローリング回収して蓄積する。

    1分足は直近7日しか遡れないため、**継続的に回収して貯める**必要がある。
    週1回でも足りるが、取りこぼしを考えて日次で回す。

    冪等であること。同じ日を再取得しても既存データを壊さない。
    """
    raise NotImplementedError("Phase 1 で実装する")
