"""Layer 2: 日次銘柄選定（寄り前 08:00-08:55）。

Layer 1 を通過した100〜200銘柄から、**その日実際に監視する50銘柄**を選ぶ。
50 という数字は WebSocket のリアルタイム配信上限に由来する。

【重要】ルックアヘッドバイアスの回避（docs/03-universe.md §4.3）

寄り前の選定に使ってよいのは、**前日大引けまでの確定情報と当日の寄り前気配だけ**。
当日の終値や日中データを使うと、バックテストだけ好成績になり本番で再現しない。
"""

from __future__ import annotations

from datetime import date

from autotrader.types import Symbol, UniverseEntry


def score(
    symbol: Symbol,
    atr_pct: float,
    gap_pct: float,
    premarket_volume_ratio: float,
    weights: dict[str, float],
) -> float:
    """日次スコアを計算する。

    各指標を順位に変換し、正規化して加重合計する。

    - ``atr_pct``: 14日ATR ÷ 株価。日中値幅の期待値
    - ``gap_pct``: 寄り前気配 vs 前日終値。初動のエネルギー
    - ``premarket_volume_ratio``: 寄り前の板の厚み ÷ 20日平均出来高。注目度の代理変数
    """
    raise NotImplementedError("Phase 2 で実装する")


def select(
    candidates: tuple[Symbol, ...],
    trade_date: date,
    max_watchlist: int = 50,
    min_atr_pct: float = 0.02,
    premium_score_multiplier: float = 1.3,
    premium_max_concurrent: int = 1,
) -> tuple[UniverseEntry, ...]:
    """当日の監視銘柄を選定する。

    手順:

    1. 各指標を計算しスコア化する
    2. **ATR% < min_atr_pct の銘柄を落とす**
       （日中値幅が往復コストの5倍ないとコスト負けする）
    3. 通常枠からスコア上位を採用する
    4. プレミアム枠は条件を満たす場合のみ追加する:

       - スコアが**通常枠採用銘柄のスコア中央値 × premium_score_multiplier 以上**
       - 同時採用は ``premium_max_concurrent`` 銘柄まで
       - 1銘柄あたり総資産25%の上限を超えないこと

    Args:
        premium_score_multiplier: プレミアム枠のハードル倍率。
            **暫定値 1.3。** Phase 4 で「プレミアム枠あり/なし」の成績を比較し、
            寄与を検証してから確定する。寄与しないなら枠ごと削除する。

    Returns:
        監視対象。最大 ``max_watchlist`` 件。
    """
    raise NotImplementedError("Phase 2 で実装する")
