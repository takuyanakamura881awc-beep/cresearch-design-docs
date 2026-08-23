"""ランダムエントリー・ベースラインのテスト。

**このベースラインが竹より有利だと比較が壊れる。**
「エントリー以外はすべて竹と同一」を構造で保証できているかを確認する。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from autotrader.strategy.random_baseline import (
    ENTRY_REASON,
    RandomEntry,
    entry_probability_for,
)
from autotrader.strategy.take_intraday import TakeIntraday, TakeIntradayConfig
from autotrader.types import Bar, MarginTradeType, Position, Side

T0 = datetime(2026, 6, 1, 9, 0)


def _bars(code: str = "7203", n: int = 40, price: float = 1000.0) -> tuple[Bar, ...]:
    out = []
    for i in range(n):
        p = price + i * 0.5
        out.append(
            Bar(
                symbol=code,
                timestamp=T0 + timedelta(minutes=5 * i),
                open=p,
                high=p * 1.01,
                low=p * 0.99,
                close=p,
                volume=10_000,
                turnover=2_000_000_000.0,
            )
        )
    return tuple(out)


def _now(minutes: int = 60) -> datetime:
    return T0 + timedelta(minutes=minutes)


class TestReproducibility:
    """**再現しない検証は検証ではない。**"""

    def test_同じシードなら同じ結果(self) -> None:
        bars = {"A": _bars("A"), "B": _bars("B", price=800.0)}
        first = RandomEntry(seed=42, entry_probability=0.5)
        second = RandomEntry(seed=42, entry_probability=0.5)
        for minute in (60, 65, 70, 75):
            a = first.generate(_now(minute), bars, ())
            b = second.generate(_now(minute), bars, ())
            assert [(s.symbol, s.side) for s in a] == [(s.symbol, s.side) for s in b]

    def test_違うシードなら結果が変わる(self) -> None:
        """全シードで同じ結果なら、分布を作る意味がなくなる。"""
        bars = {f"S{i}": _bars(f"S{i}") for i in range(20)}
        outputs = set()
        for seed in range(8):
            strategy = RandomEntry(seed=seed, entry_probability=0.3)
            picked = strategy.generate(_now(), bars, ())
            outputs.add(tuple((s.symbol, s.side.value) for s in picked))
        assert len(outputs) > 1

    def test_銘柄の並び順に依存しない(self) -> None:
        """dict の順序で結果が変わると、同じシードでも再現しない。"""
        forward = {"A": _bars("A"), "B": _bars("B"), "C": _bars("C")}
        reverse = {"C": _bars("C"), "B": _bars("B"), "A": _bars("A")}
        a = RandomEntry(seed=7, entry_probability=0.5).generate(_now(), forward, ())
        b = RandomEntry(seed=7, entry_probability=0.5).generate(_now(), reverse, ())
        assert [s.symbol for s in a] == [s.symbol for s in b]


class TestSameAsTakeExceptEntry:
    """**竹との違いがエントリーだけであること。**"""

    def test_手仕舞いは竹と同一の実装(self) -> None:
        """継承しているので `should_close` は同じコード。

        書き写していたら、片方を直したときにもう片方が置き去りになる。
        """
        assert RandomEntry.should_close is TakeIntraday.should_close

    def test_ストップと利確の幅が竹と一致する(self) -> None:
        cfg = TakeIntradayConfig()
        bars = {"A": _bars("A")}
        signals = RandomEntry(seed=1, entry_probability=1.0, config=cfg).generate(
            _now(), bars, ()
        )
        assert signals
        signal = signals[0]
        assert signal.stop_price is not None
        assert signal.take_profit_price is not None
        last = bars["A"][-1].close
        stop_width = abs(last - signal.stop_price)
        profit_width = abs(signal.take_profit_price - last)
        # 1.5 : 2.5 の比が保たれていること
        assert profit_width / stop_width == pytest.approx(
            cfg.take_profit_atr_mult / cfg.stop_loss_atr_mult
        )

    def test_ショートには必ずストップが載る(self) -> None:
        """安全装置 #3。ここが緩いとランダム側だけ発注が通ってしまう。"""
        bars = {f"S{i}": _bars(f"S{i}") for i in range(30)}
        shorts = 0
        for seed in range(10):
            for signal in RandomEntry(seed=seed, entry_probability=1.0).generate(
                _now(), bars, ()
            ):
                if signal.side is Side.SHORT:
                    shorts += 1
                    assert signal.stop_price is not None
        assert shorts > 0, "ショートが1件も出ていない。テストになっていない"

    def test_建玉のある銘柄には入らない(self) -> None:
        bars = {"A": _bars("A")}
        held = (
            Position(
                symbol="A",
                side=Side.LONG,
                quantity=100,
                entry_price=1000.0,
                margin_trade_type=MarginTradeType.DAYTRADE,
                opened_at=T0,
            ),
        )
        assert RandomEntry(seed=1, entry_probability=1.0).generate(_now(), bars, held) == ()

    def test_オープニングレンジ確定前は入らない(self) -> None:
        """竹は09:30まで入らない。揃えないとランダム側だけ有利になる。"""
        bars = {"A": _bars("A")}
        strategy = RandomEntry(seed=1, entry_probability=1.0)
        assert strategy.generate(T0 + timedelta(minutes=10), bars, ()) == ()
        assert strategy.config.range_end == time(9, 30)

    def test_1銘柄1日1回まで(self) -> None:
        """竹の再エントリー制限を継承していること。"""
        bars = {"A": _bars("A")}
        strategy = RandomEntry(seed=1, entry_probability=1.0)
        first = strategy.generate(_now(60), bars, ())
        assert first
        # 同じ日に2回目は出ない（建玉がなくても）
        assert strategy.generate(_now(65), bars, ()) == ()

    def test_シグナル名が竹と混ざらない(self) -> None:
        bars = {"A": _bars("A")}
        signals = RandomEntry(seed=1, entry_probability=1.0).generate(_now(), bars, ())
        assert all(s.reason == ENTRY_REASON for s in signals)


class TestEntryProbability:
    def test_トレード数から確率を出す(self) -> None:
        assert entry_probability_for(500, 50, 1000) == pytest.approx(0.01)

    def test_1を超えない(self) -> None:
        assert entry_probability_for(10_000, 2, 3) == 1.0

    def test_不正な入力を拒否する(self) -> None:
        with pytest.raises(ValueError, match="銘柄数"):
            entry_probability_for(100, 0, 100)
        with pytest.raises(ValueError, match="目標トレード数"):
            entry_probability_for(0, 10, 100)

    def test_確率の範囲を検証する(self) -> None:
        with pytest.raises(ValueError, match="エントリー確率"):
            RandomEntry(seed=1, entry_probability=0.0)
        with pytest.raises(ValueError, match="エントリー確率"):
            RandomEntry(seed=1, entry_probability=1.5)
