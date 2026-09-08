"""autotrader.execution_model のテスト。

**このモジュールが答えるのは「スプレッドを払うのか受け取るのか」。**
「何円か」は `autotrader.tick` の担当で、そちらは別テスト。

重点は3つ:

1. **板寄せの取り分がゼロであること。** 単一価格で約定するので気配を跨がない
   ——ここが崩れると、寄成で建てられる手法の優位を測り間違える
2. **既定が保守側（払う側）のままであること。** 意思決定ログ103 で
   「板寄せ版は感度としてのみ出し、既定は動かさない」と決めた
3. **円換算は `tick` をそのまま使うこと。** コストモデルを二重に持たない
"""

from __future__ import annotations

import pytest

from autotrader.execution_model import (
    DEFAULT_ENTRY,
    DEFAULT_EXIT,
    EntryStyle,
    ExitStyle,
    round_trip_cost_bps,
    spread_multiple,
)
from autotrader.tick import DEFAULT_SPREAD_TICKS, spread_yen


class TestSpreadMultiple:
    def test_成行往復はスプレッド1本(self) -> None:
        """**これまでの全実験がこの想定で測られている。**"""
        assert spread_multiple(EntryStyle.MARKET, ExitStyle.MARKET) == pytest.approx(1.0)

    def test_板寄せで建てると半分になる(self) -> None:
        """板寄せは単一価格なので気配を跨がない。払うのは手仕舞いだけ。"""
        assert spread_multiple(EntryStyle.AUCTION, ExitStyle.MARKET) == pytest.approx(0.5)

    def test_指値で建てられれば往復ゼロ(self) -> None:
        """**上限。** 逆選択も未約定も入っていない。"""
        assert spread_multiple(EntryStyle.PASSIVE, ExitStyle.MARKET) == pytest.approx(0.0)

    def test_両端が板寄せならゼロ(self) -> None:
        """引成は安全装置#2 に反するので既定にはしないが、計算は通る。"""
        assert spread_multiple(EntryStyle.AUCTION, ExitStyle.AUCTION) == pytest.approx(0.0)

    def test_既定は払う側(self) -> None:
        """**保守側に倒す**（規約5）。板寄せ版は感度としてのみ出す。"""
        assert DEFAULT_ENTRY is EntryStyle.MARKET
        assert DEFAULT_EXIT is ExitStyle.MARKET
        assert spread_multiple() == pytest.approx(1.0)


class TestRoundTripCostBps:
    def test_既定はtickのスプレッド1本と一致する(self) -> None:
        """**コストモデルを二重に持たない**（規約「同じことをする関数を二つ作らない」）。"""
        price = 2_000.0
        expected = float(spread_yen(price, DEFAULT_SPREAD_TICKS)) / price * 10_000.0
        assert round_trip_cost_bps(price) == pytest.approx(expected)

    def test_板寄せで建てると半分になる(self) -> None:
        price = 2_000.0
        full = round_trip_cost_bps(price)
        half = round_trip_cost_bps(price, entry=EntryStyle.AUCTION)
        assert half == pytest.approx(full / 2.0)

    def test_指値で建てられればゼロ(self) -> None:
        assert round_trip_cost_bps(2_000.0, entry=EntryStyle.PASSIVE) == pytest.approx(0.0)

    def test_受け取り側は負になりうる(self) -> None:
        """引成まで使えば受け取り超過。**符号をそのまま返す（丸めない）。**"""
        cost = round_trip_cost_bps(
            2_000.0, entry=EntryStyle.PASSIVE, exit_=ExitStyle.AUCTION
        )
        assert cost < 0

    def test_TOPIX100は呼値が細かいぶん安い(self) -> None:
        """呼値のテーブルは `tick` 側。ここでは素通しされることだけ見る。"""
        price = 2_000.0
        assert round_trip_cost_bps(price, topix100=True) < round_trip_cost_bps(price)

    def test_呼値の本数を変えると比例する(self) -> None:
        price = 2_000.0
        one = round_trip_cost_bps(price, n_ticks=1.0)
        two = round_trip_cost_bps(price, n_ticks=2.0)
        assert two == pytest.approx(one * 2.0)

    @pytest.mark.parametrize("price", [0.0, -1.0])
    def test_価格が0以下ならエラー(self, price: float) -> None:
        """**握り潰さない**（規約「エラーを握り潰さない」）。"""
        with pytest.raises(ValueError):
            round_trip_cost_bps(price)
