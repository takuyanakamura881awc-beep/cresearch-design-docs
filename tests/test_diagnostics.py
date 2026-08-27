"""autotrader.diagnostics のテスト。

**日クラスタ統計は3つの診断スクリプトが共有している。** ここが壊れると、
`measure_gap_fade` / `measure_intraday_fill` / `measure_overnight_reversal`
のすべてで t値が嘘になる。

重点は2つ:

1. **件数を増やしても日数が同じなら t値が変わらないこと。**
   これが日クラスタを導入した理由そのもの（意思決定ログ72）
2. **期間分割が日で切れること。** 同じ日の観測が前半と後半に
   またがってはいけない
"""

from __future__ import annotations

from datetime import date

import pytest

from autotrader.diagnostics import clustered_stats, split_days

DAY1 = date(2026, 6, 1)
DAY2 = date(2026, 6, 2)
DAY3 = date(2026, 6, 3)
DAY4 = date(2026, 6, 4)


class TestClusteredStats:
    def test_実質的な標本数は日数(self) -> None:
        samples = [(day, 10.0 + i) for day in (DAY1, DAY2) for i in range(50)]
        stats = clustered_stats(samples)
        assert stats is not None
        assert stats.days == 2

    def test_日ごとに等ウェイトにする(self) -> None:
        """**該当銘柄が多い日を過大に扱わない。**

        DAY1 は9件すべて +100、DAY2 は1件だけ -100。件数平均なら +80 に
        寄るが、日ごとに等ウェイトなら 0 になる。
        """
        samples = [(DAY1, 100.0)] * 9 + [(DAY2, -100.0)]
        stats = clustered_stats(samples)
        assert stats is not None
        assert stats.mean_bps == pytest.approx(0.0)

    def test_件数を増やしても日数が同じならt値は変わらない(self) -> None:
        """**これが日クラスタを入れた理由そのもの**（意思決定ログ72）。

        同じ2日を1日9件で見ても18件で見ても、市場の動きが同じなら
        得られた情報は増えていない。
        """

        def build(per_day: int) -> list[tuple[date, float]]:
            return [
                (day, value)
                for day, value in ((DAY1, 20.0), (DAY2, 10.0))
                for _ in range(per_day)
            ]

        small = clustered_stats(build(9))
        large = clustered_stats(build(18))
        assert small is not None and large is not None
        assert small.t_stat == pytest.approx(large.t_stat)
        assert small.mean_bps == pytest.approx(large.mean_bps)

    def test_日をまたいだばらつきだけがt値を作る(self) -> None:
        """日ごとの平均が同じなら、日内のばらつきがいくらあっても t値は出ない。"""
        samples = [(DAY1, 0.0), (DAY1, 20.0), (DAY2, -10.0), (DAY2, 30.0)]
        stats = clustered_stats(samples)
        assert stats is not None
        assert stats.mean_bps == pytest.approx(10.0)
        assert stats.t_stat == 0.0

    def test_符号は保たれる(self) -> None:
        negative = clustered_stats([(DAY1, -30.0), (DAY2, -10.0)])
        assert negative is not None
        assert negative.mean_bps < 0
        assert negative.t_stat < 0

    def test_該当日が2日未満ならNone(self) -> None:
        assert clustered_stats([(DAY1, 10.0)] * 100) is None
        assert clustered_stats([]) is None


class TestSplitDays:
    def test_日で二分する(self) -> None:
        first, second = split_days((DAY1, DAY2, DAY3, DAY4))
        assert first == frozenset({DAY1, DAY2})
        assert second == frozenset({DAY3, DAY4})

    def test_重ならず合わせて全期間になる(self) -> None:
        days = (DAY1, DAY2, DAY3)
        first, second = split_days(days)
        assert first.isdisjoint(second)
        assert first | second == frozenset(days)

    def test_重複した日は一度だけ数える(self) -> None:
        """**同じ日の観測が複数あっても、日としては1つ。**"""
        first, second = split_days([DAY1] * 10 + [DAY2] * 10)
        assert first == frozenset({DAY1})
        assert second == frozenset({DAY2})

    def test_順序が乱れていても正しく分ける(self) -> None:
        first, second = split_days((DAY3, DAY1, DAY4, DAY2))
        assert first == frozenset({DAY1, DAY2})
        assert second == frozenset({DAY3, DAY4})

    def test_日数が奇数でも取りこぼさない(self) -> None:
        first, second = split_days((DAY1, DAY2, DAY3))
        assert len(first) + len(second) == 3
        assert first and second

    def test_1日しかなければ分けない(self) -> None:
        first, second = split_days((DAY1,))
        assert first == frozenset({DAY1})
        assert second == frozenset()

    def test_空なら空を返す(self) -> None:
        assert split_days(()) == (frozenset(), frozenset())
