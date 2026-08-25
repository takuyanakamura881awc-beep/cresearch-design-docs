"""竹（メイン手法）のテスト。

重点:

1. **矛盾するシグナルで見送ること** — AとBは方向が逆になりうる
2. **未確定のオープニングレンジで発火しないこと** — 形成中に抜けても意味がない
3. **VWAP が当日始まりでリセットされること** — 前日を引きずると乖離が常に大きい
4. **ショートに必ずストップが載ること**（安全装置 #3）
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from autotrader.strategy.take_intraday import (
    TakeIntraday,
    TakeIntradayConfig,
    opening_range,
    vwap,
)
from autotrader.types import Bar, MarginTradeType, Position, Side

DAY = date(2026, 6, 1)
OPEN = datetime(2026, 6, 1, 9, 0)


def _bar(
    minute: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: int = 10_000,
    day_offset: int = 0,
) -> Bar:
    return Bar(
        symbol="7203",
        timestamp=OPEN + timedelta(days=day_offset, minutes=minute),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
    )


def _history(n: int = 20, close: float = 1000.0, spread: float = 10.0) -> list[Bar]:
    """ATR を計算できるだけの前日ぶんの履歴。"""
    return [
        _bar(i * 5, close, high=close + spread / 2, low=close - spread / 2, day_offset=-1)
        for i in range(n)
    ]


def _position(side: Side = Side.LONG, entry: float = 1000.0) -> Position:
    return Position(
        symbol="7203",
        side=side,
        quantity=100,
        entry_price=entry,
        margin_trade_type=MarginTradeType.DAYTRADE,
        opened_at=OPEN,
    )


class TestOpeningRange:
    def test_9時から9時半の高安を取る(self) -> None:
        bars = tuple(
            _bar(m, 1000.0, high=1000.0 + m, low=1000.0 - m) for m in (0, 5, 10, 25, 35)
        )
        rng = opening_range(bars, datetime(2026, 6, 1, 10, 0))
        assert rng is not None
        # 9:35 のバーはレンジ外なので 9:25 までの高安
        assert rng.high == 1025.0
        assert rng.low == 975.0

    def test_形成中は未確定になる(self) -> None:
        """**未確定のレンジで発火させない。** 形成中に抜けても意味がない。"""
        bars = tuple(_bar(m, 1000.0) for m in (0, 5, 10))
        assert opening_range(bars, datetime(2026, 6, 1, 9, 15)) is not None
        assert not opening_range(bars, datetime(2026, 6, 1, 9, 15)).complete  # type: ignore[union-attr]
        assert opening_range(bars, datetime(2026, 6, 1, 9, 30)).complete  # type: ignore[union-attr]

    def test_前日のバーを混ぜない(self) -> None:
        """レンジは「その日の」値。前日を混ぜると意味が変わる。"""
        bars = (*_history(spread=200.0), _bar(0, 1000.0, high=1005.0, low=995.0))
        rng = opening_range(bars, datetime(2026, 6, 1, 9, 30))
        assert rng is not None
        assert rng.high == 1005.0

    def test_当日のバーがなければNone(self) -> None:
        assert opening_range(tuple(_history()), datetime(2026, 6, 1, 9, 30)) is None
        assert opening_range((), datetime(2026, 6, 1, 9, 30)) is None


class TestVwap:
    def test_出来高で加重する(self) -> None:
        bars = (
            _bar(0, 1000.0, high=1000.0, low=1000.0, volume=1_000),
            _bar(5, 1100.0, high=1100.0, low=1100.0, volume=3_000),
        )
        assert vwap(bars, DAY) == pytest.approx((1000 * 1000 + 1100 * 3000) / 4000)

    def test_当日始まりでリセットする(self) -> None:
        """**前日を引きずると寄り付き直後の乖離が常に大きく出る。**"""
        bars = (*_history(close=500.0), _bar(0, 1000.0))
        assert vwap(bars, DAY) == pytest.approx(1000.0)

    def test_当日のバーがなければNone(self) -> None:
        assert vwap(tuple(_history()), DAY) is None

    def test_出来高ゼロならNone(self) -> None:
        """ゼロ除算を戦略側に漏らさない。"""
        assert vwap((_bar(0, 1000.0, volume=0),), DAY) is None


class TestConfig:
    def test_利確は損切りより大きくなければならない(self) -> None:
        """逆だと勝率が高くても期待値が負になる。"""
        with pytest.raises(ValueError, match="利確倍率"):
            TakeIntradayConfig(stop_loss_atr_mult=2.5, take_profit_atr_mult=1.5)

    def test_ブレイクアウトの余裕をゼロにできない(self) -> None:
        """レンジ端に触れただけで発火するとノイズを全部拾う。"""
        with pytest.raises(ValueError, match="breakout_buffer_bps"):
            TakeIntradayConfig(breakout_buffer_bps=0.0)

    def test_レンジの時刻の前後関係を検証する(self) -> None:
        with pytest.raises(ValueError):
            TakeIntradayConfig(range_start=time(10, 0), range_end=time(9, 0))

    def test_既定値はstrategiesのyamlと一致する(self) -> None:
        cfg = TakeIntradayConfig()
        assert cfg.range_start == time(9, 0) and cfg.range_end == time(9, 30)
        assert cfg.breakout_buffer_bps == 5
        assert cfg.min_deviation_pct == 0.015
        assert cfg.stop_loss_atr_mult == 1.5
        assert cfg.take_profit_atr_mult == 2.5
        assert cfg.max_holding_minutes == 180
        assert cfg.volume_confirmation_mult is None

    def test_出来高確認倍率をゼロ以下にできない(self) -> None:
        with pytest.raises(ValueError, match="volume_confirmation_mult"):
            TakeIntradayConfig(volume_confirmation_mult=0.0)


class TestBreakout:
    """シグナルA — オープニングレンジ・ブレイクアウト（順張り）。"""

    def _bars(self, breakout_close: float, *, now_minute: int = 40) -> tuple[Bar, ...]:
        session = [
            _bar(m, 1000.0, high=1010.0, low=990.0) for m in range(0, 30, 5)
        ]  # 9:00-9:25 → レンジ 990〜1010
        session.append(_bar(now_minute, breakout_close))
        return (*_history(), *session)

    def test_レンジ上抜けでロング(self) -> None:
        signals = TakeIntraday().generate(
            datetime(2026, 6, 1, 9, 45), {"7203": self._bars(1020.0)}, ()
        )
        assert len(signals) == 1
        assert signals[0].side is Side.LONG
        assert signals[0].reason == "orb"

    def test_レンジ下抜けでショート(self) -> None:
        signals = TakeIntraday().generate(
            datetime(2026, 6, 1, 9, 45), {"7203": self._bars(980.0)}, ()
        )
        assert signals[0].side is Side.SHORT

    def test_反転スイッチで向きだけが逆になる(self) -> None:
        """**仮説検定用のスイッチ。** 発火条件は変えず向きだけ反転する。

        条件も変えてしまうと「順張りが悪いのか条件が悪いのか」が分離できない。
        """
        inverted = TakeIntradayConfig(invert_breakout=True)
        up = TakeIntraday(inverted).generate(
            datetime(2026, 6, 1, 9, 45), {"7203": self._bars(1020.0)}, ()
        )
        down = TakeIntraday(inverted).generate(
            datetime(2026, 6, 1, 9, 45), {"7203": self._bars(980.0)}, ()
        )
        # 上抜けで売り / 下抜けで買い
        assert up[0].side is Side.SHORT
        assert down[0].side is Side.LONG
        # **発火する場面は変わらない**
        assert up[0].reason == down[0].reason == "orb"

    def test_反転しても発火しない場面は同じ(self) -> None:
        inverted = TakeIntradayConfig(invert_breakout=True)
        assert (
            TakeIntraday(inverted).generate(
                datetime(2026, 6, 1, 9, 45), {"7203": self._bars(1000.0)}, ()
            )
            == ()
        )

    def test_ブレイクを切るとorbが1件も出ない(self) -> None:
        """VWAP乖離だけを分離して測るためのスイッチ。"""
        disabled = TakeIntradayConfig(enable_breakout=False)
        signals = TakeIntraday(disabled).generate(
            datetime(2026, 6, 1, 9, 45), {"7203": self._bars(1020.0)}, ()
        )
        assert all("orb" not in s.reason for s in signals)

    def test_レンジ内では発火しない(self) -> None:
        assert (
            TakeIntraday().generate(
                datetime(2026, 6, 1, 9, 45), {"7203": self._bars(1000.0)}, ()
            )
            == ()
        )

    def test_端に触れただけでは発火しない(self) -> None:
        """余裕（5bps）を超える必要がある。"""
        assert (
            TakeIntraday().generate(
                datetime(2026, 6, 1, 9, 45), {"7203": self._bars(1010.0)}, ()
            )
            == ()
        )

    def test_レンジ形成中は発火しない(self) -> None:
        """**9:30 より前は抜けても発火させない。**"""
        bars = self._bars(1020.0, now_minute=20)
        assert TakeIntraday().generate(datetime(2026, 6, 1, 9, 25), {"7203": bars}, ()) == ()

    def test_ショートを禁止できる(self) -> None:
        strategy = TakeIntraday(TakeIntradayConfig(allow_short=False))
        assert (
            strategy.generate(datetime(2026, 6, 1, 9, 45), {"7203": self._bars(980.0)}, ())
            == ()
        )

    def test_保有中の銘柄には出さない(self) -> None:
        signals = TakeIntraday().generate(
            datetime(2026, 6, 1, 9, 45), {"7203": self._bars(1020.0)}, (_position(),)
        )
        assert signals == ()


class TestSignals:
    def test_ショートには必ずストップが載る(self) -> None:
        """**安全装置 #3。** 載っていないと ReplayBroker が発注を拒否する。"""
        session = [_bar(m, 1000.0, high=1010.0, low=990.0) for m in range(0, 30, 5)]
        session.append(_bar(40, 980.0))
        signals = TakeIntraday().generate(
            datetime(2026, 6, 1, 9, 45), {"7203": (*_history(), *session)}, ()
        )
        assert signals[0].stop_price is not None
        assert signals[0].stop_price > signals[0].take_profit_price  # type: ignore[operator]

    def test_ロングのストップは下利確は上(self) -> None:
        session = [_bar(m, 1000.0, high=1010.0, low=990.0) for m in range(0, 30, 5)]
        session.append(_bar(40, 1020.0))
        signal = TakeIntraday().generate(
            datetime(2026, 6, 1, 9, 45), {"7203": (*_history(), *session)}, ()
        )[0]
        assert signal.stop_price is not None and signal.stop_price < 1020.0
        assert signal.take_profit_price is not None and signal.take_profit_price > 1020.0

    def test_ATRが計算できなければ出さない(self) -> None:
        """履歴が足りない銘柄でストップ幅を決められない。"""
        assert TakeIntraday().generate(
            datetime(2026, 6, 1, 9, 45), {"7203": (_bar(0, 1000.0),)}, ()
        ) == ()

    def test_矛盾するシグナルは見送る(self) -> None:
        """**AとBが逆方向に発火したらエントリーしない。**

        レンジを上抜け（A: ロング）しつつ VWAP から上方乖離が縮小
        （B: ショート）している状況を作る。
        """
        # 9:00-9:25 は 1000 付近で薄い出来高、9:30 以降に大商いで急騰
        session = [
            _bar(m, 1000.0, high=1002.0, low=998.0, volume=1_000) for m in range(0, 30, 5)
        ]
        session.append(_bar(30, 1100.0, volume=1_000))   # 上抜け + 上方乖離が拡大
        session.append(_bar(35, 1060.0, volume=1_000))   # 乖離が縮小に転じる
        bars = (*_history(), *session)

        strategy = TakeIntraday()
        assert strategy._breakout(bars, datetime(2026, 6, 1, 9, 40), session[-1]) is Side.LONG
        assert strategy._reversion(bars, datetime(2026, 6, 1, 9, 40), session[-1]) is Side.SHORT
        assert strategy.generate(datetime(2026, 6, 1, 9, 40), {"7203": bars}, ()) == ()

    def test_見送りを無効にできる(self) -> None:
        """検証で「見送りに意味があるか」を比較するための逃げ道。"""
        session = [
            _bar(m, 1000.0, high=1002.0, low=998.0, volume=1_000) for m in range(0, 30, 5)
        ]
        session.append(_bar(30, 1100.0, volume=1_000))
        session.append(_bar(35, 1060.0, volume=1_000))
        strategy = TakeIntraday(TakeIntradayConfig(skip_on_conflicting_signals=False))
        signals = strategy.generate(
            datetime(2026, 6, 1, 9, 40), {"7203": (*_history(), *session)}, ()
        )
        assert len(signals) == 1


class TestReversion:
    """シグナルB — VWAP乖離の平均回帰（逆張り）。"""

    def test_拡大中は入らない(self) -> None:
        """**落ちるナイフを掴まない。** 乖離が縮小に転じてから入る。"""
        session = [
            _bar(0, 1000.0, volume=1_000),
            _bar(5, 1030.0, volume=1_000),
            _bar(10, 1080.0, volume=1_000),  # 乖離が拡大し続けている
        ]
        strategy = TakeIntraday()
        assert strategy._reversion(
            (*_history(), *session), datetime(2026, 6, 1, 9, 15), session[-1]
        ) is None

    def test_乖離が小さければ入らない(self) -> None:
        session = [_bar(0, 1000.0, volume=1_000), _bar(5, 1005.0, volume=1_000)]
        strategy = TakeIntraday()
        assert strategy._reversion(
            (*_history(), *session), datetime(2026, 6, 1, 9, 10), session[-1]
        ) is None

    def test_縮小確認を外せる(self) -> None:
        session = [
            _bar(0, 1000.0, volume=1_000),
            _bar(5, 1030.0, volume=1_000),
            _bar(10, 1080.0, volume=1_000),
        ]
        strategy = TakeIntraday(TakeIntradayConfig(require_deviation_shrinking=False))
        assert strategy._reversion(
            (*_history(), *session), datetime(2026, 6, 1, 9, 15), session[-1]
        ) is Side.SHORT

    def test_出来高が平均未満なら見送る(self) -> None:
        """**薄商いの乖離はノイズの可能性が高い。** 出来高で確認できなければ入らない。"""
        session = [
            _bar(0, 1000.0, volume=5_000),
            _bar(5, 1030.0, volume=5_000),
            _bar(10, 1080.0, volume=1_000),  # それまでの平均5,000の1.5倍(7,500)未満
        ]
        strategy = TakeIntraday(
            TakeIntradayConfig(require_deviation_shrinking=False, volume_confirmation_mult=1.5)
        )
        assert strategy._reversion(
            (*_history(), *session), datetime(2026, 6, 1, 9, 15), session[-1]
        ) is None

    def test_出来高が平均以上なら発火する(self) -> None:
        session = [
            _bar(0, 1000.0, volume=1_000),
            _bar(5, 1030.0, volume=1_000),
            _bar(10, 1080.0, volume=5_000),  # それまでの平均1,000の1.5倍(1,500)以上
        ]
        strategy = TakeIntraday(
            TakeIntradayConfig(require_deviation_shrinking=False, volume_confirmation_mult=1.5)
        )
        assert strategy._reversion(
            (*_history(), *session), datetime(2026, 6, 1, 9, 15), session[-1]
        ) is Side.SHORT


class TestShouldClose:
    def _bars(self, close: float) -> tuple[Bar, ...]:
        """履歴13本の True Range が10、最終バーは高安ゼロなので TR = |値動き|。

        **最終バーのギャップ自体が ATR を押し上げる**ので、
        しきい値は「ATR 10 × 倍率」より大きくなる::

            ATR = (13×10 + |move|) / 14
            発火条件 |move| >= 倍率 × ATR  →  |move| >= 130×倍率 / (14 − 倍率)

            損切り(1.5倍) → 15.60円   利確(2.5倍) → 28.26円
        """
        return (*_history(spread=10.0), _bar(0, close, high=close, low=close))

    def test_損切り幅に達したらクローズ(self) -> None:
        should, reason = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(), self._bars(984.0)  # -16円
        )
        assert should and reason == "stop"

    def test_利確幅に達したらクローズ(self) -> None:
        should, reason = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(), self._bars(1029.0)  # +29円
        )
        assert should and reason == "take_profit"

    def test_境界の内側では保持する(self) -> None:
        """15.60円 / 28.26円 のわずか内側では発火しない。"""
        should, _ = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(), self._bars(985.0)  # -15円
        )
        assert not should
        should, _ = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(), self._bars(1028.0)  # +28円
        )
        assert not should

    def test_ショートは方向が反転する(self) -> None:
        should, reason = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(Side.SHORT), self._bars(971.0)  # -29円
        )
        assert should and reason == "take_profit"

        should, reason = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(Side.SHORT), self._bars(1016.0)
        )
        assert should and reason == "stop"

    def test_時間切れでクローズ(self) -> None:
        should, reason = TakeIntraday().should_close(
            datetime(2026, 6, 1, 12, 5), _position(), self._bars(1000.0)
        )
        assert should and reason == "time_exit"

    def test_どれにも当たらなければ保持(self) -> None:
        should, reason = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(), self._bars(1005.0)
        )
        assert not should and reason == "hold"

    def test_バーがなければ保持(self) -> None:
        should, _ = TakeIntraday().should_close(
            datetime(2026, 6, 1, 10, 0), _position(), ()
        )
        assert not should


class TestReentry:
    """**再エントリー制御。**

    実データ（39営業日）で無制限にしたところ、損切り直後に
    シグナルAが再発火して入り直す往復が起き、1日28.6トレードまで膨らんだ
    （想定は3〜5）。損切り83件の多くがこの往復だった。
    """

    def _breaking(self) -> tuple[Bar, ...]:
        session = [_bar(m, 1000.0, high=1010.0, low=990.0) for m in range(0, 30, 5)]
        session.append(_bar(40, 1020.0))  # レンジ上抜け
        return (*_history(), *session)

    def _now(self) -> datetime:
        return datetime(2026, 6, 1, 9, 45)

    def test_同じ銘柄に1日1回しか入らない(self) -> None:
        strategy = TakeIntraday()
        bars = {"7203": self._breaking()}
        assert len(strategy.generate(self._now(), bars, ())) == 1
        assert strategy.generate(self._now(), bars, ()) == ()

    def test_損切りした銘柄にその日もう入らない(self) -> None:
        """**損切りは「その日のその仮説が外れた」という証拠。**"""
        strategy = TakeIntraday(
            TakeIntradayConfig(max_entries_per_symbol_per_day=5)
        )
        bars = {"7203": self._breaking()}
        assert len(strategy.generate(self._now(), bars, ())) == 1

        # 損切りを起こす（ATR 10 相当・-16円で発火）
        should, reason = strategy.should_close(
            datetime(2026, 6, 1, 10, 0),
            _position(),
            (*_history(spread=10.0), _bar(0, 984.0)),
        )
        assert should and reason == "stop"
        assert strategy.generate(datetime(2026, 6, 1, 10, 5), bars, ()) == ()

    def test_利確なら再入場できる(self) -> None:
        """仮説が機能したので再発火を認める。ただし回数上限はかかる。"""
        strategy = TakeIntraday(
            TakeIntradayConfig(max_entries_per_symbol_per_day=2)
        )
        bars = {"7203": self._breaking()}
        assert len(strategy.generate(self._now(), bars, ())) == 1

        should, reason = strategy.should_close(
            datetime(2026, 6, 1, 10, 0),
            _position(),
            (*_history(spread=10.0), _bar(0, 1029.0)),
        )
        assert should and reason == "take_profit"
        assert len(strategy.generate(datetime(2026, 6, 1, 10, 5), bars, ())) == 1
        # 2回で上限
        assert strategy.generate(datetime(2026, 6, 1, 10, 10), bars, ()) == ()

    def test_翌日にはリセットされる(self) -> None:
        """**前日の損切りを引きずると翌日の正当なシグナルまで殺す。**"""
        strategy = TakeIntraday()
        bars = {"7203": self._breaking()}
        strategy.generate(self._now(), bars, ())
        strategy.should_close(
            datetime(2026, 6, 1, 10, 0), _position(), (*_history(spread=10.0), _bar(0, 984.0))
        )
        assert strategy.generate(datetime(2026, 6, 1, 10, 5), bars, ()) == ()

        # 翌日ぶんのバーを作って同じ状況を再現
        next_day = [
            _bar(m, 1000.0, high=1010.0, low=990.0, day_offset=1) for m in range(0, 30, 5)
        ]
        next_day.append(_bar(40, 1020.0, day_offset=1))
        assert len(
            strategy.generate(
                datetime(2026, 6, 2, 9, 45), {"7203": (*_history(), *next_day)}, ()
            )
        ) == 1

    def test_許可すれば再エントリーできる(self) -> None:
        """検証で「禁止に意味があるか」を比較するための逃げ道。"""
        strategy = TakeIntraday(
            TakeIntradayConfig(reenter_after_stop=True, max_entries_per_symbol_per_day=3)
        )
        bars = {"7203": self._breaking()}
        strategy.generate(self._now(), bars, ())
        strategy.should_close(
            datetime(2026, 6, 1, 10, 0), _position(), (*_history(spread=10.0), _bar(0, 984.0))
        )
        assert len(strategy.generate(datetime(2026, 6, 1, 10, 5), bars, ())) == 1

    def test_回数上限はゼロを許さない(self) -> None:
        with pytest.raises(ValueError):
            TakeIntradayConfig(max_entries_per_symbol_per_day=0)
