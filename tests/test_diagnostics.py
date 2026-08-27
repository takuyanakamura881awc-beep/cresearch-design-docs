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

from datetime import date, timedelta

import pytest

from autotrader.diagnostics import (
    clustered_stats,
    non_overlapping_days,
    required_gross_bps,
    split_days,
)

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


class TestRequiredGrossBps:
    """**基準ではなく算術。** 目標とコストと建玉率から一意に決まる。"""

    def test_コストを足した値になる(self) -> None:
        without = required_gross_bps(0.25, cost_bps=0.0)
        with_cost = required_gross_bps(0.25, cost_bps=4.8)
        assert with_cost == pytest.approx(without + 4.8)

    def test_建玉率が低いほど多くの優位が要る(self) -> None:
        """**資金の半分しか建玉になっていなければ、2倍の優位が要る。**"""
        full = required_gross_bps(0.25, cost_bps=0.0, deployment=1.0)
        half = required_gross_bps(0.25, cost_bps=0.0, deployment=0.5)
        assert half == pytest.approx(full * 2)

    def test_目標が高いほど多くの優位が要る(self) -> None:
        assert required_gross_bps(0.35, cost_bps=4.8) > required_gross_bps(
            0.25, cost_bps=4.8
        )

    def test_年利ゼロならコストぶんだけ要る(self) -> None:
        """損益分岐＝コストを取り返すだけ。"""
        assert required_gross_bps(0.0, cost_bps=4.8) == pytest.approx(4.8)

    def test_複利で割り戻す(self) -> None:
        """**単利で240営業日で割らない。** 年利25%は日次0.0093%であって0.104%ではない。"""
        daily_bps = required_gross_bps(0.25, cost_bps=0.0)
        assert daily_bps == pytest.approx(9.3, abs=0.1)
        assert daily_bps < 0.25 / 240 * 10_000

    def test_TOPIX100と通常銘柄で桁が変わる(self) -> None:
        """**呼値の差がそのまま合格ラインの差になる**（意思決定ログ67）。"""
        topix100 = required_gross_bps(0.25, cost_bps=4.8)
        regular = required_gross_bps(0.25, cost_bps=21.0)
        assert topix100 == pytest.approx(14.1, abs=0.1)
        assert regular == pytest.approx(30.3, abs=0.1)

    def test_建玉率が0以下ならエラー(self) -> None:
        with pytest.raises(ValueError, match="deployment"):
            required_gross_bps(0.25, cost_bps=4.8, deployment=0.0)


class TestNonOverlappingDays:
    """**重なる保有期間は独立ではない。**

    3日保有を毎日建てると、D日とD+1日の玉が同じ日を共有する。
    重なったまま t値を出すと約√N倍 過大に出る（意思決定ログ90）。
    """

    def _days(self, n: int) -> list[date]:
        return [date(2026, 6, 1) + timedelta(days=i) for i in range(n)]

    def test_保有1日なら全日を返す(self) -> None:
        """**重なりが無いので間引かない。**"""
        days = self._days(10)
        assert non_overlapping_days(days, 1) == frozenset(days)

    def test_保有3日なら3日おきに採る(self) -> None:
        days = self._days(9)
        assert non_overlapping_days(days, 3) == frozenset(
            {days[0], days[3], days[6]}
        )

    def test_独立な観測は日数を保有期間で割った数になる(self) -> None:
        """**これが t値を√N倍過大にしていた原因。**"""
        days = self._days(100)
        assert len(non_overlapping_days(days, 1)) == 100
        assert len(non_overlapping_days(days, 5)) == 20
        assert len(non_overlapping_days(days, 10)) == 10

    def test_重複した日は一度だけ数える(self) -> None:
        """同じ日に複数銘柄の観測があっても、日としては1つ。"""
        days = self._days(6)
        assert non_overlapping_days(days * 10, 2) == frozenset(
            {days[0], days[2], days[4]}
        )

    def test_順序が乱れていても正しく間引く(self) -> None:
        days = self._days(6)
        assert non_overlapping_days(reversed(days), 2) == frozenset(
            {days[0], days[2], days[4]}
        )

    def test_保有期間が日数を超えれば先頭だけ(self) -> None:
        days = self._days(3)
        assert non_overlapping_days(days, 10) == frozenset({days[0]})

    def test_空なら空(self) -> None:
        assert non_overlapping_days((), 5) == frozenset()

    def test_保有期間が0以下ならエラー(self) -> None:
        with pytest.raises(ValueError, match="horizon"):
            non_overlapping_days(self._days(5), 0)

    def test_位相をずらすと別の日が選ばれる(self) -> None:
        """**点推定に使ってはいけない理由。** どの日から数え始めたかに依存する。"""
        days = self._days(6)
        assert non_overlapping_days(days, 3, phase=0) == frozenset(
            {days[0], days[3]}
        )
        assert non_overlapping_days(days, 3, phase=1) == frozenset(
            {days[1], days[4]}
        )
        assert non_overlapping_days(days, 3, phase=2) == frozenset(
            {days[2], days[5]}
        )

    def test_全位相を合わせると全日になる(self) -> None:
        """**位相ごとの平均を集めれば、全体の平均と同じ標本を覆う。**"""
        days = self._days(9)
        covered: set[date] = set()
        for phase in range(3):
            covered |= non_overlapping_days(days, 3, phase)
        assert covered == set(days)

    def test_位相が範囲外ならエラー(self) -> None:
        days = self._days(6)
        with pytest.raises(ValueError, match="phase"):
            non_overlapping_days(days, 3, phase=3)
        with pytest.raises(ValueError, match="phase"):
            non_overlapping_days(days, 3, phase=-1)

    def test_重なりを外すとt値が下がる(self) -> None:
        """**実測で 2.7 → 1.6 相当に落ちた**のと同じ構造を合成データで固定する。"""
        days = self._days(60)
        # 同じ値の繰り返しではばらつきが0になるので、日ごとに少しずつ変える
        samples = [(d, 10.0 + (i % 7)) for i, d in enumerate(days)]
        overlapping = clustered_stats(samples)
        kept = non_overlapping_days(days, 5)
        independent = clustered_stats([(d, v) for d, v in samples if d in kept])
        assert overlapping is not None and independent is not None
        assert independent.days < overlapping.days
        assert abs(independent.t_stat) < abs(overlapping.t_stat)
