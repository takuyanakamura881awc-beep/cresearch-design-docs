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

from autotrader.diagnostics import clustered_stats, required_gross_bps, split_days

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
