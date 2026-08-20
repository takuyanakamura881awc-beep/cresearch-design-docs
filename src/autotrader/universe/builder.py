"""Layer 1: ユニバース構築（日次バッチ、寄り前 07:00 実行）。

東証プライム約1,600銘柄から、取引対象になりうる母集団を機械的に絞る。
想定通過数は100〜200銘柄（Phase 2 で実データ検証する）。

【重要】サバイバーシップバイアスの回避（docs/03-universe.md §4.2）

「**現在**プライムに上場している銘柄」で過去を検証すると、
上場廃止・降格した銘柄が母集団から抜け落ち、成績が構造的に過大評価される。

``as_of`` を受け取り、**その時点の上場銘柄一覧**（J-Quants の日付指定取得）から
ユニバースを再構成すること。現在の銘柄一覧を過去に適用してはならない。
"""

from __future__ import annotations

from datetime import date

from autotrader.types import Symbol


def build(as_of: date) -> tuple[Symbol, ...]:
    """指定日時点のユニバースを構築する。

    Args:
        as_of: 基準日。**この日時点で知り得た情報だけを使うこと。**
            未来のデータを参照するとバックテストだけ好成績になる。

    Returns:
        フィルタを通過した銘柄。想定は100〜200銘柄。
    """
    raise NotImplementedError("Phase 2 で実装する")
