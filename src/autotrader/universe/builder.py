"""Layer 1: ユニバース構築（日次バッチ、寄り前 07:00 実行）。

東証プライムから、取引対象になりうる母集団を機械的に絞る。

実測（2026-08-23 / 基準日 2026-05-29。``scripts/measure_universe.py``）::

    全上場 4,451
      → プライム              1,565   （A: ETF は市場「その他」でここに落ちる）
      → かつ 貸借銘柄         1,483   （D）
      → ストップ高安を除く    1,476   （H）
      → 株価 300〜1,250円       ---   （C: 上限は資金と25%上限から導出）
      → 売買代金 3億円以上      133   （B）← Layer 1 の出力

**株価上限とレバレッジ上限の食い違いを実測で発見して直した経緯**:

当初は株価上限3,000円 / 売買代金10億円で287銘柄だったが、1銘柄あたり25%
（docs/05 #7）を守ると買える最大株価は1,250円で、**上限超の502銘柄は
選定を通ってもサイジングで0株になっていた**。株価上限を1,250円に直すと
55銘柄まで落ちたため、安全装置ではない流動性下限を10億→3億に下げて
133銘柄まで戻した（docs/03-universe.md §1）。

**サバイバーシップバイアスの回避（docs/03-universe.md §4.2）**

「**現在**プライムに上場している銘柄」で過去を検証すると、
上場廃止・降格した銘柄が母集団から抜け落ち、成績が構造的に過大評価される。

``as_of`` を受け取り、**その時点の上場銘柄一覧**（J-Quants の
``equities/master`` は日付指定に対応）からユニバースを再構成する。
現在の銘柄一覧を過去に適用してはならない。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from autotrader.data.base import BarDataSource
from autotrader.types import Bar, Symbol
from autotrader.universe.filters import (
    FilterConfig,
    RejectReason,
    ScreenResult,
    screen,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UniverseSnapshot:
    """ある時点のユニバースと、その絞り込みの内訳。

    **内訳を持つのが要点。** 最終的な銘柄数が想定と食い違ったとき、
    どのフィルタで落ちたかが分からないと打つ手を決められない。

    実際にこれで判断できた: 実測287（想定100〜200）に対し、内訳から
    「流動性664・株価上限502が主役」と分かり、**閾値ではなく想定のほうが
    誤りだった**と結論できた。
    """

    as_of: date
    passed: tuple[ScreenResult, ...]
    rejected: tuple[ScreenResult, ...]
    total_listed: int
    """``as_of`` 時点の全上場銘柄数"""
    reject_counts: dict[RejectReason, int] = field(default_factory=dict)

    @property
    def symbols(self) -> tuple[str, ...]:
        """通過した銘柄コード。"""
        return tuple(r.symbol for r in self.passed)

    @property
    def size(self) -> int:
        return len(self.passed)

    def summary(self) -> str:
        """内訳を人が読める形にする。実測スクリプトと日次ログで使う。"""
        lines = [
            f"ユニバース {self.as_of}: 全上場 {self.total_listed} → 通過 {self.size}"
        ]
        for reason, count in sorted(
            self.reject_counts.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  除外 {reason.value:<18} {count:>5}")
        return "\n".join(lines)


def build(
    as_of: date,
    source: BarDataSource,
    config: FilterConfig | None = None,
    *,
    bars_by_symbol: dict[str, tuple[Bar, ...]] | None = None,
    symbols: tuple[Symbol, ...] | None = None,
) -> UniverseSnapshot:
    """指定日時点のユニバースを構築する。

    Args:
        as_of: 基準日。**この日時点で知り得た情報だけを使う。**
            未来のデータを参照するとバックテストだけ好成績になる。
        source: 銘柄一覧の取得元（J-Quants）。
        bars_by_symbol: 銘柄コード → ``as_of`` までの日足。
            事前に一括取得したものを渡す（5件/分の制約下で銘柄ごとに
            取りに行くのは非現実的）。省略した場合は日足判定をスキップし、
            市場区分と信用区分だけで絞る。
        symbols: 銘柄一覧を外から渡す場合（テストや再計算用）。

    Returns:
        ユニバースと絞り込みの内訳。
    """
    cfg = config or FilterConfig()

    if symbols is None:
        listed = source.list_symbols(as_of)
        if listed is None:
            raise ValueError(
                f"データソース {source.name} は銘柄一覧を提供しない。"
                "サバイバーシップバイアスの回避には日付指定の一覧が必須"
            )
        symbols = listed

    bars_map = bars_by_symbol or {}
    passed: list[ScreenResult] = []
    rejected: list[ScreenResult] = []
    counts: Counter[RejectReason] = Counter()

    for symbol in symbols:
        result = screen(symbol, bars_map.get(symbol.code, ()), cfg)
        if result.passed:
            passed.append(result)
        else:
            rejected.append(result)
            if result.reason is not None:
                counts[result.reason] += 1

    snapshot = UniverseSnapshot(
        as_of=as_of,
        passed=tuple(passed),
        rejected=tuple(rejected),
        total_listed=len(symbols),
        reject_counts=dict(counts),
    )
    logger.info("%s", snapshot.summary())
    return snapshot


def bars_lookback_start(
    as_of: date, config: FilterConfig | None = None, *, margin: int = 10
) -> date:
    """判定に必要な日足の取得開始日。

    20日平均売買代金には20営業日ぶんが要る。土日祝を考慮して暦日で余裕を持たせる
    （営業日カレンダーが無くても足りるだけの幅を取る）。

    Args:
        margin: 祝日ぶんの追加余裕（暦日）。
    """
    cfg = config or FilterConfig()
    # 20営業日 ≒ 28暦日。土日で約1.4倍になるので係数を掛ける
    calendar_days = int(cfg.turnover_lookback_days * 1.5) + margin
    return as_of - timedelta(days=calendar_days)
