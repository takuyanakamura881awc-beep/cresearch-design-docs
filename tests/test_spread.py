"""Corwin-Schultz スプレッド推定のテスト。

重点は4つ。

1. **合成データで真のスプレッドを復元できること**——数式を写し間違えて
   いないことの、最も直接的な確認
2. **β・γ を先に平均してから解いていること**——1ペアずつ解いて平均すると
   推定が壊れる（`autotrader.spread` の docstring に実測あり）。
   この順序が守られているかを、性質として固定する
3. **夜間ギャップを補正すること**——補正しないと2日通しの高安が
   ギャップぶん広がり、過大推定になる
4. **負の推定値を0に切り上げないこと**——負は「推定が効いていない」
   合図であり、0とみなすと使えない値を使ってしまう
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

import pytest

from autotrader.spread import corwin_schultz, spread_from_beta_gamma
from autotrader.types import Bar

T0 = datetime(2026, 1, 1)


def _bar(day: int, *, high: float, low: float) -> Bar:
    close = (high + low) / 2
    return Bar(
        symbol="X",
        timestamp=T0 + timedelta(days=day),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=10_000,
    )


def _synthetic(
    true_spread_pct: float,
    *,
    n_days: int = 3000,
    steps: int = 500,
    seed: int = 1,
    day_vol: float = 0.015,
) -> tuple[Bar, ...]:
    """真のスプレッドを与えて日足を作る。

    効率的価格をランダムウォークさせ、その日の高安に**スプレッドの半分ずつ**を
    上下に乗せる（高値＝最良売気配、安値＝最良買気配というモデル）。

    ``steps`` は1日の値動きの刻み数。Corwin-Schultz は高安が連続的に
    観測されることを前提とするので、**粗すぎると下振れする**
    （それ自体が推定量の性質で、docstring に記録してある）。
    """
    rng = random.Random(seed)
    price = 1000.0
    bars: list[Bar] = []
    half = true_spread_pct / 2.0
    for d in range(n_days):
        high = low = price
        for _ in range(steps):
            price *= math.exp(rng.gauss(0.0, day_vol / math.sqrt(steps)))
            high = max(high, price)
            low = min(low, price)
        bars.append(_bar(d, high=high * (1 + half), low=low * (1 - half)))
    return tuple(bars)


class TestSpreadFromBetaGamma:
    def test_原著の式どおりに解く(self) -> None:
        beta, gamma = 0.0004, 0.0006
        k = 3.0 - 2.0 * math.sqrt(2.0)
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
        expected = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
        assert spread_from_beta_gamma(beta, gamma) == pytest.approx(expected)

    def test_負を切り上げない(self) -> None:
        """**負は「推定が効いていない」合図。** 0とみなすと使えない値を使う。"""
        # γ が β に対して大きすぎると α が負になる
        assert spread_from_beta_gamma(0.0001, 0.01) < 0


class TestCorwinSchultz:
    def test_合成データで真のスプレッドを復元する(self) -> None:
        """**数式を写し間違えていないことの直接の確認。**

        推定量の性質としてわずかに下振れするので、幅を持たせて確認する
        （`autotrader.spread` の docstring に実測値あり）。
        """
        for true_pct in (0.001, 0.002, 0.004):
            estimate = corwin_schultz(_synthetic(true_pct))
            assert estimate is not None
            assert estimate.usable
            # 下振れ側に3bpsまでのずれを許容し、上振れは厳しく見る
            assert true_pct - 0.0004 < estimate.spread_pct <= true_pct + 0.0002

    def test_スプレッドが広いほど推定値も大きい(self) -> None:
        """単調性。**個別の絶対値より、大小関係が保たれることが重要。**"""
        narrow = corwin_schultz(_synthetic(0.001))
        wide = corwin_schultz(_synthetic(0.004))
        assert narrow is not None and wide is not None
        assert wide.spread_pct > narrow.spread_pct

    def test_1ペアずつ解いて平均する実装になっていない(self) -> None:
        """**β・γ を先に平均してから解いていることを、性質として固定する。**

        1ペアずつ解いて負を0に切り上げてから平均すると、真が5bpsでも
        47bps といった桁違いの上振れが出る（docstring の実測）。
        真のスプレッドが十分小さい合成データで、推定がその水準に
        収まっていれば「平均してから解いている」と言える。
        """
        estimate = corwin_schultz(_synthetic(0.0005))
        assert estimate is not None
        # 1ペアずつ解く実装なら 40bps 超になる。10bps 未満なら平均が先
        assert estimate.spread_bps < 10.0

    def test_夜間ギャップの大きさが推定に影響しない(self) -> None:
        """**補正の核心はここ。** 日中の値幅が同じなら、夜間にいくら
        飛ぼうと推定は変わらないこと。

        補正しなければ2日通しの高安がギャップぶん広がり、飛ぶほど
        過大推定になる。補正は「重ならないぶんだけ平行移動して
        隣接させる」ので、**ギャップ幅そのものは打ち消される**。

        なお補正後も2日通しのレンジは1日ぶんより広い——それは
        ギャップではなく**実際に方向へ動いた**ぶんなので消さない。
        """
        small_gap = (
            _bar(0, high=1010.0, low=990.0),
            _bar(1, high=1110.0, low=1090.0),  # 80円ぶん飛んだ
        )
        huge_gap = (
            _bar(0, high=1010.0, low=990.0),
            _bar(1, high=1510.0, low=1490.0),  # 480円ぶん飛んだ（日中の値幅は同じ20円）
        )
        small_est = corwin_schultz(small_gap)
        huge_est = corwin_schultz(huge_gap)
        assert small_est is not None and huge_est is not None
        assert huge_est.spread_pct == pytest.approx(small_est.spread_pct)

    def test_下へのギャップも補正する(self) -> None:
        """上に飛んだ場合だけでなく、下に飛んだ場合も同じように扱う。"""
        down_small = (
            _bar(0, high=1010.0, low=990.0),
            _bar(1, high=910.0, low=890.0),
        )
        down_huge = (
            _bar(0, high=1010.0, low=990.0),
            _bar(1, high=510.0, low=490.0),
        )
        small_est = corwin_schultz(down_small)
        huge_est = corwin_schultz(down_huge)
        assert small_est is not None and huge_est is not None
        assert huge_est.spread_pct == pytest.approx(small_est.spread_pct)

    def test_バーが1本ならNone(self) -> None:
        assert corwin_schultz((_bar(0, high=1010.0, low=990.0),)) is None

    def test_バーが空ならNone(self) -> None:
        assert corwin_schultz(()) is None

    def test_安値が0以下のペアは除外する(self) -> None:
        bars = (_bar(0, high=10.0, low=0.0), _bar(1, high=10.0, low=5.0))
        assert corwin_schultz(bars) is None

    def test_高値と安値が逆転したペアは除外する(self) -> None:
        broken = Bar(
            symbol="X",
            timestamp=T0,
            open=1000.0,
            high=990.0,
            low=1010.0,
            close=1000.0,
            volume=10_000,
        )
        assert corwin_schultz((broken, _bar(1, high=1010.0, low=990.0))) is None

    def test_並び順に依存しない(self) -> None:
        """`BarStore.read` の返す順が保証されなくても同じ結果になる。"""
        ordered = (
            _bar(0, high=1010.0, low=990.0),
            _bar(1, high=1020.0, low=1000.0),
            _bar(2, high=1015.0, low=995.0),
        )
        shuffled = (ordered[2], ordered[0], ordered[1])
        a = corwin_schultz(ordered)
        b = corwin_schultz(shuffled)
        assert a is not None and b is not None
        assert a.spread_pct == pytest.approx(b.spread_pct)
        assert a.n_pairs == b.n_pairs

    def test_値幅ゼロの日が続けばスプレッドゼロ付近になる(self) -> None:
        """高安が動かない＝観測できる値動きもスプレッドもない。"""
        bars = tuple(_bar(d, high=1000.0, low=1000.0) for d in range(10))
        estimate = corwin_schultz(bars)
        assert estimate is not None
        assert estimate.spread_pct == pytest.approx(0.0, abs=1e-9)


class TestSpreadEstimate:
    def test_bps換算(self) -> None:
        estimate = corwin_schultz(_synthetic(0.002))
        assert estimate is not None
        assert estimate.spread_bps == pytest.approx(estimate.spread_pct * 10_000)

    def test_負ならusableがFalse(self) -> None:
        """**負を「使える推定値」として扱わない。**"""
        # γ が大きくなる（2日通しで大きく動く）が日中は動かない合成
        bars = (
            _bar(0, high=1000.5, low=1000.0),
            _bar(1, high=1200.0, low=1199.5),
        )
        estimate = corwin_schultz(bars)
        assert estimate is not None
        assert estimate.spread_pct < 0
        assert not estimate.usable
