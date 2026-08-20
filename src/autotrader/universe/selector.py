"""Layer 2: 日次銘柄選定。

Layer 1 を通過した100〜200銘柄から、**その日実際に監視する50銘柄**を選ぶ。

【上限50の由来】
Stage B の WebSocket リアルタイム配信の制限。
Stage A（ヒストリカル検証）には技術的な制約はないが、**同じ50に揃える**。
銘柄数を変えると成績が変わり、Stage A の検証結果が Stage B に引き継げなくなるため。

【重要】ルックアヘッドバイアスの回避（docs/03-universe.md §4.3）

寄り前の選定に使ってよいのは、**前日大引けまでの確定情報と
（Stage B のみ）当日の寄り前気配だけ**。
当日の終値や日中データを使うと、バックテストだけ好成績になり本番で再現しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from autotrader.types import Symbol, UniverseEntry


@dataclass(frozen=True)
class StageAFeatures:
    """Stage A のスコア指標。**前日引け時点の情報のみで構成する。**

    寄り前気配は kabuステーションAPI が必要なため Stage A では使えない
    （docs/09-data-sources.md §5）。

    これは妥協ではない。「前日引け時点の情報だけで翌日の監視銘柄を決める」のは
    実運用でも一般的な方式で、寄り前気配は流動性が薄くノイズが多いという批判もある。
    """

    atr_pct: float
    """14日ATR ÷ 株価。日中値幅の期待値"""
    prev_volume_ratio: float
    """前日出来高 ÷ 20日平均出来高。前日に注目が集まったか"""
    prev_range_pct: float
    """(前日高値 − 前日安値) ÷ 前日終値。ボラティリティの持続性"""
    prev_close_position: float
    """(終値 − 安値) ÷ (高値 − 安値)。前日の需給の決着"""


@dataclass(frozen=True)
class StageBFeatures:
    """Stage B で追加される指標。寄り前気配が必要。

    Stage B ではこれを追加して**予測力を比較検証する**:

    - Stage A スコア単体
    - Stage A スコア + 寄り前気配

    **予測力を持たないなら追加しない。**
    プレミアム枠の寄与検証と同じ構造で判定する。
    """

    gap_pct: float
    """寄り前気配 vs 前日終値。初動のエネルギー"""
    premarket_volume_ratio: float
    """寄り前気配の板の厚み ÷ 20日平均出来高。当日の注目度の代理変数"""


def score(
    features: StageAFeatures,
    weights: dict[str, float],
    stage_b: StageBFeatures | None = None,
) -> float:
    """日次スコアを計算する。

    各指標を順位に変換し、正規化して加重合計する。

    Args:
        features: Stage A の指標（前日引け時点）。
        weights: 指標名 → 重み。
        stage_b: Stage B の追加指標。``None`` なら Stage A のスコアのみ。

    Note:
        Stage B で指標を追加するときは、**Stage A で確定した重みを流用しない**。
        指標が変われば最適な重みも変わるため、
        ウォークフォワード検証をやり直すこと（docs/03-universe.md §4.4）。
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
       （Stage A は前日引け時点の4指標、Stage B は寄り前気配を追加）
    2. **ATR% < min_atr_pct の銘柄を落とす**
       （日中値幅が往復コストの5倍ないとコスト負けする）
    3. 通常枠からスコア上位を採用する
    4. プレミアム枠は条件を満たす場合のみ追加する:

       - スコアが**通常枠採用銘柄のスコア中央値 × premium_score_multiplier 以上**
       - 同時採用は ``premium_max_concurrent`` 銘柄まで
       - 1銘柄あたり総資産25%の上限を超えないこと

    Args:
        premium_score_multiplier: プレミアム枠のハードル倍率。
            **暫定値 1.3。** Phase 3 で「プレミアム枠あり/なし」の成績を比較し、
            寄与を検証してから確定する。寄与しないなら枠ごと削除する。

    Returns:
        監視対象。最大 ``max_watchlist`` 件。
    """
    raise NotImplementedError("Phase 2 で実装する")
