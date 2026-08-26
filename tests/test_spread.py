"""Corwin-Schultz スプレッド推定のテスト。

重点は5つ。

1. **合成データで真のスプレッドを復元できること**——数式を写し間違えて
   いないことの、最も直接的な確認
2. **夜間ギャップがあっても推定が壊れないこと**——ここが実データで
   最初に踏んだ落とし穴（意思決定ログ58）。原著どおりの補正では
   133銘柄すべてが負になった
3. **β・γ を先に平均してから解いていること**——1ペアずつ解いて平均すると
   推定が壊れる（`autotrader.spread` の docstring に実測あり）
4. **負の推定値を0に切り上げないこと**——負は「推定が効いていない」
   合図であり、0とみなすと使えない値を使ってしまう
5. 境界条件（価格0以下・高安の逆転・バー不足）で落ちないこと
"""

from __future__ import annotations

import math
import random
import statistics
from datetime import datetime, timedelta

import pytest

from autotrader.spread import (
    corwin_schultz,
    corwin_schultz_pooled,
    spread_from_beta_gamma,
)
from autotrader.types import Bar

T0 = datetime(2026, 1, 1)


def _bar(
    day: int,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    close: float | None = None,
) -> Bar:
    """日足を1本作る。

    ``open_`` / ``close`` を省くと高安の中央にする。**夜間ギャップの
    補正は前日終値と当日始値を使う**ので、ギャップを試すテストでは
    明示的に渡すこと。
    """
    mid = (high + low) / 2
    return Bar(
        symbol="X",
        timestamp=T0 + timedelta(days=day),
        open=mid if open_ is None else open_,
        high=high,
        low=low,
        close=mid if close is None else close,
        volume=10_000,
    )


def _synthetic(
    true_spread_pct: float,
    *,
    n_days: int = 3000,
    steps: int = 500,
    seed: int = 1,
    day_vol: float = 0.02,
    overnight_vol: float = 0.01,
) -> tuple[Bar, ...]:
    """真のスプレッドを与えて日足を作る。

    効率的価格をランダムウォークさせ、その日の高安に**スプレッドの半分ずつ**を
    上下に乗せる（高値＝最良売気配、安値＝最良買気配というモデル）。

    **夜間ギャップを既定で入れている**（``overnight_vol``）。実データで
    推定を壊したのがこれなので、合成データ側にも入れておかないと
    テストが素通りしてしまう。

    ``steps`` は1日の値動きの刻み数。Corwin-Schultz は高安が連続的に
    観測されることを前提とするので、**粗すぎると下振れする**
    （それ自体が推定量の性質で、docstring に記録してある）。
    """
    rng = random.Random(seed)
    price = 1000.0
    bars: list[Bar] = []
    half = true_spread_pct / 2.0
    for d in range(n_days):
        price *= math.exp(rng.gauss(0.0, overnight_vol))  # 夜間の飛び
        open_price = price
        high = low = price
        for _ in range(steps):
            price *= math.exp(rng.gauss(0.0, day_vol / math.sqrt(steps)))
            high = max(high, price)
            low = min(low, price)
        bars.append(
            _bar(
                d,
                high=high * (1 + half),
                low=low * (1 - half),
                open_=open_price,
                close=price,
            )
        )
    return tuple(bars)


class TestSpreadFromBetaGamma:
    def test_原著の式どおりに解く(self) -> None:
        beta, gamma = 0.0004, 0.0006
        k = 3.0 - 2.0 * math.sqrt(2.0)
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
        expected = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
        assert spread_from_beta_gamma(beta, gamma) == pytest.approx(expected)

    def test_betaとgammaが等しければゼロ(self) -> None:
        """**式の校正点。** 2日ぶんの値動きが1日ぶんの2倍ちょうどなら
        スプレッド成分は無い、というのがこの推定量の建て付け。"""
        assert spread_from_beta_gamma(0.001, 0.001) == pytest.approx(0.0, abs=1e-12)

    def test_負を切り上げない(self) -> None:
        """**負は「推定が効いていない」合図。** 0とみなすと使えない値を使う。"""
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
            # 下振れ側に5bpsまでのずれを許容し、上振れは厳しく見る
            assert true_pct - 0.0005 < estimate.spread_pct <= true_pct + 0.0002

    def test_夜間ギャップが大きくても推定が壊れない(self) -> None:
        """**実データで最初に踏んだ落とし穴（意思決定ログ58）。**

        原著どおり「高安が重ならないときだけ平行移動」する補正では、
        夜間ボラ1%で推定が -34bps まで振り切れた。前日終値→当日始値の
        比で除く実装なら、夜間ボラを変えても推定は動かないはず。
        """
        estimates = [
            corwin_schultz(_synthetic(0.002, overnight_vol=vol))
            for vol in (0.0, 0.01, 0.02, 0.03)
        ]
        assert all(e is not None and e.usable for e in estimates)
        values = [e.spread_pct for e in estimates if e is not None]
        # 夜間ボラを0%から3%まで振っても、推定はほぼ動かない
        assert max(values) - min(values) < 0.0002

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

    def test_夜間に飛んでも日中の値幅が同じなら推定は同じ(self) -> None:
        """**補正の核心。** 日中レンジが同じなら、夜間にいくら飛ぼうと
        推定は変わらないこと。飛び幅は終値→始値の比で正確に除かれる。
        """
        # 日中は 990〜1010 を動き、1000 で引ける2日。夜間の飛びだけを変える
        def two_days(gap_factor: float) -> tuple[Bar, ...]:
            base = 1000.0
            nxt = base * gap_factor
            return (
                _bar(0, high=1010.0, low=990.0, open_=1000.0, close=1000.0),
                _bar(
                    1,
                    high=nxt * 1.01,
                    low=nxt * 0.99,
                    open_=nxt,
                    close=nxt,
                ),
            )

        small = corwin_schultz(two_days(1.01))  # 1%の飛び
        huge = corwin_schultz(two_days(1.50))  # 50%の飛び
        down = corwin_schultz(two_days(0.60))  # 40%下に飛ぶ
        assert small is not None and huge is not None and down is not None
        assert huge.spread_pct == pytest.approx(small.spread_pct)
        assert down.spread_pct == pytest.approx(small.spread_pct)

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

    def test_始値がゼロでも落ちない(self) -> None:
        """始値・終値が使えないときは原著の近似に落ちる。**例外にしない。**"""
        bars = (
            _bar(0, high=1010.0, low=990.0, close=1000.0),
            _bar(1, high=1020.0, low=1000.0, open_=0.0, close=1010.0),
        )
        estimate = corwin_schultz(bars)
        assert estimate is not None  # 補正の経路が変わるだけで、計算はできる

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


class TestCorwinSchultzPooled:
    """**銘柄ごとに推定して正だけ平均すると壊れる**ので、まとめて解く経路。"""

    def test_合成データで真のスプレッドを復元する(self) -> None:
        bars_by_symbol = {
            f"S{i}": _synthetic(0.002, n_days=400, seed=i) for i in range(8)
        }
        pooled = corwin_schultz_pooled(bars_by_symbol)
        assert pooled is not None
        assert pooled.usable
        assert 0.0015 < pooled.spread_pct <= 0.0022

    def test_負を捨てないので銘柄ごとの平均より低く出る(self) -> None:
        """**これがこの関数の存在理由。**

        銘柄ごとの推定は半分近くが負になる。正だけ拾うとノイズで
        上振れした銘柄だけが残るので、まとめて解いた値より高く出る。
        真のスプレッドが十分小さいときにその差が現れる。
        """
        bars_by_symbol = {
            f"S{i}": _synthetic(0.0, n_days=300, seed=100 + i) for i in range(12)
        }
        pooled = corwin_schultz_pooled(bars_by_symbol)
        assert pooled is not None

        per_symbol = [
            e.spread_pct
            for bars in bars_by_symbol.values()
            if (e := corwin_schultz(bars)) is not None
        ]
        positives = [v for v in per_symbol if v > 0]
        if positives:
            # 真が0なのに「正のみ」は正の値を返す。まとめた方が真に近い
            assert pooled.spread_pct < statistics.median(positives)

    def test_n_pairsは全銘柄の合計(self) -> None:
        bars_by_symbol = {
            "A": tuple(_bar(d, high=1010.0 + d, low=990.0 + d) for d in range(5)),
            "B": tuple(_bar(d, high=2010.0 + d, low=1990.0 + d) for d in range(4)),
        }
        pooled = corwin_schultz_pooled(bars_by_symbol)
        assert pooled is not None
        assert pooled.n_pairs == 4 + 3

    def test_銘柄が空ならNone(self) -> None:
        assert corwin_schultz_pooled({}) is None

    def test_使えるペアがなければNone(self) -> None:
        assert corwin_schultz_pooled({"A": (_bar(0, high=1010.0, low=990.0),)}) is None


class TestSpreadEstimate:
    def test_bps換算(self) -> None:
        estimate = corwin_schultz(_synthetic(0.002))
        assert estimate is not None
        assert estimate.spread_bps == pytest.approx(estimate.spread_pct * 10_000)

    def test_負ならusableがFalse(self) -> None:
        """**負を「使える推定値」として扱わない。**

        日中はほとんど動かないのに、2日通しでは大きく動いた形を作る
        （夜間の飛びではなく、始値・終値も動いている＝日中の値動きが
        高安に現れていない矛盾した形）。γ が β を大きく上回り α が負になる。
        """
        bars = (
            _bar(0, high=1000.1, low=1000.0, open_=1000.0, close=1000.0),
            _bar(1, high=1100.1, low=1100.0, open_=1000.0, close=1100.0),
        )
        estimate = corwin_schultz(bars)
        assert estimate is not None
        assert estimate.spread_pct < 0
        assert not estimate.usable

    def test_n_pairsは使えたペア数(self) -> None:
        bars = tuple(_bar(d, high=1010.0 + d, low=990.0 + d) for d in range(5))
        estimate = corwin_schultz(bars)
        assert estimate is not None
        assert estimate.n_pairs == 4
