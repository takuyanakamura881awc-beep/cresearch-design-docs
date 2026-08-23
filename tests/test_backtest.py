"""バックテストエンジンのテスト。

**最重要は PointInTimeView。** ルックアヘッドを「注意して書く」のではなく
「起こせない」ことをここで担保する（docs/03-universe.md §4.3）。
backtest-validator は、注意で防いでいるだけの実装を不合格とする。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from autotrader.data.calendar import TradingCalendar
from autotrader.engine.backtest import (
    STAGE_A_SLIPPAGE_BPS,
    BacktestConfig,
    BacktestResult,
    CostModel,
    PointInTimeView,
    run,
    walk_forward,
)
from autotrader.strategy.base import Strategy
from autotrader.types import Bar, Position, Side, Signal, Trade


def _bar(code: str, minute: int, close: float = 1000.0) -> Bar:
    """9:00 起点で ``minute`` 分後に**開始する**5分足。"""
    return Bar(
        symbol=code,
        timestamp=datetime(2026, 6, 1, 9, 0) + timedelta(minutes=minute),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1000,
    )


BARS = {
    "7203": tuple(_bar("7203", m, 1000.0 + m) for m in (0, 5, 10, 15, 20)),
    "6758": tuple(_bar("6758", m, 2000.0 + m) for m in (0, 5, 10)),
}


class TestPointInTimeView:
    def test_開始時刻が現在より前のバーだけ見える(self) -> None:
        """**タイムスタンプは期間の開始時刻。**

        09:10 のバーは 09:10〜09:15 を表すので、09:10 時点では未確定。
        含めると、これから起きる5分間の高値・安値・終値が見えてしまう。
        """
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        seen = view.get("7203")
        assert [b.timestamp.minute for b in seen] == [0, 5]

    def test_最初の時刻では何も見えない(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 0))
        assert view.get("7203") == ()
        assert view.latest("7203") is None

    def test_全期間を過ぎれば全部見える(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 23, 0))
        assert len(view.get("7203")) == 5

    def test_直近の確定バーを返す(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 12))
        latest = view.latest("7203")
        assert latest is not None
        assert latest.timestamp.minute == 10

    def test_知らない銘柄には空を返す(self) -> None:
        """履歴がまだない銘柄と扱いを揃える。戦略は「足りなければ何もしない」だけでよい。"""
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        assert view.get("9999") == ()
        assert view.latest("9999") is None

    def test_戦略に渡す辞書も切り詰められている(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        as_dict = view.as_dict()
        assert set(as_dict) == {"7203", "6758"}
        assert all(
            b.timestamp < datetime(2026, 6, 1, 9, 10)
            for bars in as_dict.values()
            for b in bars
        )

    def test_未来のバーは辞書経由でも漏れない(self) -> None:
        """**as_dict は「全データを渡す抜け道」であってはならない。**"""
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 5))
        assert len(view.as_dict()["7203"]) == 1

    def test_二度呼んでも同じ結果になる(self) -> None:
        """境界をキャッシュしているので、キャッシュが結果を変えないこと。"""
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        assert view.get("7203") == view.get("7203")

    def test_元データを書き換えない(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        view.get("7203")
        assert len(BARS["7203"]) == 5


class TestCostModel:
    def test_既定はStageAの20bps(self) -> None:
        """板がないぶん Stage B（10bps）より厚く見積もる。"""
        assert CostModel().slippage_bps == STAGE_A_SLIPPAGE_BPS == 20.0

    def test_ゼロを拒否する(self) -> None:
        """**手数料が0でもコストは0ではない**（CLAUDE.md 規約5）。"""
        with pytest.raises(ValueError, match="0以下"):
            CostModel(slippage_bps=0.0)
        with pytest.raises(ValueError):
            CostModel(slippage_bps=-1.0)

    @pytest.mark.parametrize(
        ("side", "opening", "expected"),
        [
            (Side.LONG, True, 1002.0),    # 新規買い → 高く約定
            (Side.LONG, False, 998.0),    # 返済売り → 安く約定
            (Side.SHORT, True, 998.0),    # 新規売り → 安く約定
            (Side.SHORT, False, 1002.0),  # 返済買い → 高く約定
        ],
    )
    def test_常に不利な側にずらす(self, side: Side, opening: bool, expected: float) -> None:
        cost = CostModel(slippage_bps=20.0)
        assert cost.fill_price(side, 1000.0, opening=opening) == pytest.approx(expected)

    def test_往復では2倍かかる(self) -> None:
        cost = CostModel(slippage_bps=20.0)
        entry = cost.fill_price(Side.LONG, 1000.0, opening=True)
        exit_ = cost.fill_price(Side.LONG, 1000.0, opening=False)
        assert (entry - exit_) / 1000.0 == pytest.approx(0.004)  # 40bps


def _trade(side: Side, entry: float, exit_: float, qty: int = 100) -> Trade:
    return Trade(
        symbol="7203",
        side=side,
        quantity=qty,
        entry_time=datetime(2026, 6, 1, 9, 0),
        entry_price=entry,
        exit_time=datetime(2026, 6, 1, 14, 50),
        exit_price=exit_,
        exit_reason="test",
    )


class TestTrade:
    def test_ロングの損益(self) -> None:
        assert _trade(Side.LONG, 1000.0, 1010.0).pnl == pytest.approx(1000.0)

    def test_ショートは方向が反転する(self) -> None:
        assert _trade(Side.SHORT, 1000.0, 990.0).pnl == pytest.approx(1000.0)
        assert _trade(Side.SHORT, 1000.0, 1010.0).pnl == pytest.approx(-1000.0)

    def test_建玉金額と損益率(self) -> None:
        t = _trade(Side.LONG, 1000.0, 1010.0)
        assert t.notional == pytest.approx(100_000.0)
        assert t.return_pct == pytest.approx(0.01)


class TestBacktestResult:
    def test_エクイティカーブから指標をまとめる(self) -> None:
        equity = [500_000.0 + i * 100 for i in range(30)]
        trades = [_trade(Side.LONG, 1000.0, 1010.0), _trade(Side.LONG, 1000.0, 995.0)]
        result = BacktestResult.from_equity(equity, trades, initial_cash=500_000.0)

        assert result.n_trades == 2
        assert result.win_rate == 0.5
        assert result.profit_factor == pytest.approx(1000.0 / 500.0)
        assert result.total_return == pytest.approx(equity[-1] / 500_000.0 - 1)
        assert result.max_drawdown == 0.0

    def test_トレードがなくても壊れない(self) -> None:
        result = BacktestResult.from_equity([500_000.0], [], initial_cash=500_000.0)
        assert result.n_trades == 0
        assert result.total_return == 0.0
        assert result.sharpe == 0.0

    def test_レバレッジ拒否を記録する(self) -> None:
        """安全装置が働いた回数。多いのはバグではなくサイジングの示唆。"""
        result = BacktestResult.from_equity(
            [500_000.0], [], initial_cash=500_000.0, rejected_by_leverage=7
        )
        assert result.rejected_by_leverage == 7


# ---------------------------------------------------------------------------
# run() のエンドツーエンド
# ---------------------------------------------------------------------------


class _BuyOnceStrategy(Strategy):
    """最初に見えたバーで1銘柄だけロングし、あとは放置する検証用の戦略。

    手仕舞いを一切要求しないので、**当日クローズが戦略の判断と独立に
    走ることの確認**に使える。
    """

    def __init__(self, symbol: str = "7203", stop_price: float | None = None) -> None:
        self.symbol = symbol
        self.stop_price = stop_price
        self.seen_bar_counts: list[int] = []

    def generate(
        self,
        now: datetime,
        bars: dict[str, tuple[Bar, ...]],
        positions: tuple[Position, ...],
    ) -> tuple[Signal, ...]:
        self.seen_bar_counts.append(len(bars.get(self.symbol, ())))
        if positions or not bars.get(self.symbol):
            return ()
        return (
            Signal(
                symbol=self.symbol,
                side=Side.LONG,
                strength=1.0,
                reason="test",
                stop_price=self.stop_price,
            ),
        )

    def should_close(
        self, now: datetime, position: Position, bars: tuple[Bar, ...]
    ) -> tuple[bool, str]:
        return False, "hold"


def _session(day: int, prices: list[float], symbol: str = "7203") -> list[Bar]:
    """9:00 から5分刻みのバー列を1日ぶん作る。"""
    return [
        Bar(
            symbol=symbol,
            timestamp=datetime(2026, 6, day, 9, 0) + timedelta(minutes=5 * i),
            open=p,
            high=p * 1.01,
            low=p * 0.99,
            close=p,
            volume=10_000,
            turnover=2_000_000_000.0,
        )
        for i, p in enumerate(prices)
    ]


class TestRun:
    def test_バーがなければ空の結果を返す(self) -> None:
        result = run(_BuyOnceStrategy(), {})
        assert result.n_trades == 0
        assert result.equity_curve == ()

    def test_建てて当日中に閉じる(self) -> None:
        """**デイトレ信用は当日決済必須。** 持ち越すと1注文2,200円。"""
        bars = {"7203": tuple(_session(1, [1000.0] * 4))}
        config = BacktestConfig(close_time=time(9, 15))
        result = run(_BuyOnceStrategy(), bars, config)

        assert result.n_trades == 1
        assert result.trades[0].exit_reason == "close_all"
        assert result.trades[0].exit_time.time() == time(9, 15)

    def test_当日クローズは戦略の判断より優先する(self) -> None:
        """戦略が should_close で False を返し続けても関係なく閉じる。"""
        strategy = _BuyOnceStrategy()
        bars = {"7203": tuple(_session(1, [1000.0] * 4))}
        result = run(strategy, bars, BacktestConfig(close_time=time(9, 10)))
        assert result.n_trades == 1

    def test_日をまたいで建玉を持ち越さない(self) -> None:
        bars = {"7203": tuple(_session(1, [1000.0] * 4) + _session(2, [1000.0] * 4))}
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(9, 15)))
        # 各日で1往復
        assert result.n_trades == 2
        assert {t.exit_time.day for t in result.trades} == {1, 2}

    def test_値動きがなければ必ず負ける(self) -> None:
        """往復コストが引かれている証拠。ここが正なら約定モデルが甘い。"""
        bars = {"7203": tuple(_session(1, [1000.0] * 4))}
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(9, 15)))
        assert result.trades[0].pnl < 0
        assert result.total_return < 0

    def test_エクイティカーブは日次で出る(self) -> None:
        """**バーごとではなく日次。**

        シャープの年率換算（252営業日）と単位を揃える。バーごとに記録すると
        5分足では約54倍に膨らんだシャープが出る。
        """
        bars = {"7203": tuple(_session(1, [1000.0] * 4) + _session(2, [1000.0] * 4))}
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(9, 15)))
        assert len(result.equity_curve) == 2

    def test_ストップのないショートは無視する(self) -> None:
        """安全装置 #3。シグナルが来ても建てない。"""

        class ShortStrategy(_BuyOnceStrategy):
            def generate(
                self,
                now: datetime,
                bars: dict[str, tuple[Bar, ...]],
                positions: tuple[Position, ...],
            ) -> tuple[Signal, ...]:
                if positions or not bars.get(self.symbol):
                    return ()
                return (Signal(self.symbol, Side.SHORT, 1.0, "test"),)

        bars = {"7203": tuple(_session(1, [1000.0] * 4))}
        result = run(ShortStrategy(), bars, BacktestConfig(close_time=time(9, 15)))
        assert result.n_trades == 0

    def test_1単元も買えない株価では建てない(self) -> None:
        """25%上限（12.5万円）を超える1単元は建てられない。

        **エラーにせず見送る。** 選定とサイジングの食い違いは
        ユニバース側で直すべきで、ここで例外にしても直らない。
        """
        bars = {"7203": tuple(_session(1, [2000.0] * 4))}  # 1単元20万円
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(9, 15)))
        assert result.n_trades == 0


class TestRunLookahead:
    """**ルックアヘッドは構造で防ぐ。** 起こせないことを確認する。"""

    def test_戦略に渡るバーは常に確定済み(self) -> None:
        """時刻 t で見えるのは t より前に閉じたバーだけ。

        1本目の時刻では0本、2本目では1本…と増える。
        同じ本数のまま先の値が見えていたら、判断と約定が同じバーになっている。
        """
        strategy = _BuyOnceStrategy()
        bars = {"7203": tuple(_session(1, [1000.0] * 4))}
        run(strategy, bars, BacktestConfig(close_time=time(23, 0)))
        assert strategy.seen_bar_counts == [0, 1, 2, 3]

    def test_将来のバーを足しても過去区間の成績が変わらない(self) -> None:
        """**同じ期間の結果は、その後に何が起きたかに依存してはならない。**

        `test_selector.py` の当日バー混入テストと同じ構造の担保。
        """
        day1 = _session(1, [1000.0, 1010.0, 1020.0, 1030.0])
        short = {"7203": tuple(day1)}
        # 2日目に大暴落を足す。1日目の成績が変わるなら未来が漏れている
        crash = _session(2, [1000.0, 500.0, 250.0, 100.0])
        long = {"7203": tuple(day1 + crash)}

        config = BacktestConfig(close_time=time(9, 15))
        a = run(_BuyOnceStrategy(), short, config)
        b = run(_BuyOnceStrategy(), long, config)

        assert a.n_trades == 1
        assert b.n_trades == 2
        assert a.trades[0] == b.trades[0]
        assert a.equity_curve == b.equity_curve[: len(a.equity_curve)]
        assert a.total_return == b.equity_curve[0] / float(a.initial_cash) - 1


class TestWalkForward:
    def _bars_and_calendar(
        self, days: int
    ) -> tuple[dict[str, tuple[Bar, ...]], TradingCalendar]:
        series: list[Bar] = []
        for d in range(1, days + 1):
            series.extend(_session(d, [1000.0] * 4))
        calendar = TradingCalendar.from_dates(
            [date(2026, 6, d) for d in range(1, days + 1)]
        )
        return {"7203": tuple(series)}, calendar

    def test_窓ごとに結果を返す(self) -> None:
        bars, calendar = self._bars_and_calendar(10)
        results = walk_forward(
            _BuyOnceStrategy(),
            bars,
            calendar,
            date(2026, 6, 1),
            date(2026, 6, 10),
            train_days=4,
            test_days=3,
            config=BacktestConfig(close_time=time(9, 15)),
        )
        # 営業日10 − 学習4 = 6 → 3日窓が2つ
        assert len(results) == 2
        assert all(r.n_trades == 3 for r in results)

    def test_学習期間の成績は返さない(self) -> None:
        """**in-sample の成績は成績として数えない。**

        取り出す口を用意すると、良く見えるほうを報告してしまう。
        """
        bars, calendar = self._bars_and_calendar(10)
        results = walk_forward(
            _BuyOnceStrategy(), bars, calendar,
            date(2026, 6, 1), date(2026, 6, 10),
            train_days=7, test_days=3,
            config=BacktestConfig(close_time=time(9, 15)),
        )
        assert len(results) == 1
        # 6/8, 6/9, 6/10 のみ（学習期間の 6/1〜6/7 は含まれない）
        assert {t.exit_time.day for t in results[0].trades} == {8, 9, 10}

    def test_窓が取れなければ空を返す(self) -> None:
        bars, calendar = self._bars_and_calendar(5)
        results = walk_forward(
            _BuyOnceStrategy(), bars, calendar,
            date(2026, 6, 1), date(2026, 6, 5),
            train_days=10, test_days=3,
        )
        assert results == ()

    def test_不正な期間を拒否する(self) -> None:
        bars, calendar = self._bars_and_calendar(5)
        with pytest.raises(ValueError):
            walk_forward(
                _BuyOnceStrategy(), bars, calendar,
                date(2026, 6, 1), date(2026, 6, 5), train_days=0,
            )


class TestBacktestConfig:
    def test_既定はStageA(self) -> None:
        config = BacktestConfig()
        assert config.slippage_bps == STAGE_A_SLIPPAGE_BPS
        assert config.close_time == time(14, 50)
        assert config.max_weight_per_symbol == 0.25

    def test_不正な設定を拒否する(self) -> None:
        with pytest.raises(ValueError):
            BacktestConfig(initial_cash=Decimal(0))
        with pytest.raises(ValueError):
            BacktestConfig(max_concurrent=0)


# ---------------------------------------------------------------------------
# 損失ブレーカーの再現（安全装置 #4/#5/#6）
# ---------------------------------------------------------------------------


class TestBreakers:
    """**ブレーカーを再現しないバックテストは、実現不可能な成績を出す。**

    実運用では止まっていた日の取引を成績に含めてしまうため。
    ここでは「入れると結果が変わる」ことを確認する — 変わらないなら
    組み込めていない。
    """

    def _crash(self) -> dict[str, tuple[Bar, ...]]:
        """建てた直後に -10% 落ちる1日。

        1単元100株を1,002円で建てて900円まで落ちると
        -10,200円 = 資金50万の -2.04% で日次ブレーカーに達する。
        """
        return {"7203": tuple(_session(1, [1000.0, 1000.0, 900.0, 900.0]))}

    def test_日次ブレーカーが発動して当日を止める(self) -> None:
        config = BacktestConfig(close_time=time(23, 0))
        result = run(_BuyOnceStrategy(), self._crash(), config)

        assert result.breaker_days == 1
        assert result.trades[0].exit_reason == "daily_breaker"

    def test_発動を切ると結果が変わる(self) -> None:
        """**切って出した成績を採用してはならない。**

        ここが同じ結果になるなら、ブレーカーが組み込めていない。
        """
        bars = self._crash()
        # 9:15 に当日クローズ。ブレーカーは 9:10 に発動するのでそちらが先に効く
        on = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(9, 15)))
        off = run(
            _BuyOnceStrategy(),
            bars,
            BacktestConfig(close_time=time(9, 15), enforce_breakers=False),
        )
        assert on.breaker_days == 1
        assert off.breaker_days == 0
        assert on.trades[0].exit_reason == "daily_breaker"
        assert off.trades[0].exit_reason == "close_all"
        assert on.trades[0].exit_time < off.trades[0].exit_time

    def test_日中の損益で判定する(self) -> None:
        """**日次終値だけで見ると、日中に割ってから戻した日を見逃す。**

        終値は始値と同じ（損益ゼロ）だが、途中で -10% を付けている。
        """
        bars = {"7203": tuple(_session(1, [1000.0, 1000.0, 900.0, 1000.0]))}
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(23, 0)))
        assert result.breaker_days == 1

    def test_翌営業日には自動復帰する(self) -> None:
        """日次ブレーカーだけは人の承認なしで復帰してよい（#4）。"""
        bars = {
            "7203": tuple(
                _session(1, [1000.0, 1000.0, 900.0, 900.0])
                + _session(2, [1000.0, 1000.0, 1000.0, 1000.0])
            )
        }
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(9, 15)))
        # 2日目も建てている = 復帰した
        assert result.n_trades == 2
        assert {t.exit_time.day for t in result.trades} == {1, 2}
        assert [t.exit_reason for t in result.trades] == ["daily_breaker", "close_all"]

    def test_発動日に建て直さない(self) -> None:
        """止めた当日に再エントリーしたらブレーカーの意味がない。"""
        bars = {"7203": tuple(_session(1, [1000.0, 1000.0, 900.0, 900.0, 900.0, 900.0]))}
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(23, 0)))
        assert result.n_trades == 1

    def test_損失が小さければ発動しない(self) -> None:
        bars = {"7203": tuple(_session(1, [1000.0, 1000.0, 995.0, 995.0]))}
        result = run(_BuyOnceStrategy(), bars, BacktestConfig(close_time=time(23, 0)))
        assert result.breaker_days == 0

    def test_連続損失で期間の途中から停止する(self) -> None:
        """**#5 は人の承認がないと再開しない。** 以降の期間は取引しない。"""
        days: list[Bar] = []
        for d in range(1, 9):
            # 毎日じわじわ負ける（往復コストだけでも負ける）
            days.extend(_session(d, [1000.0] * 4))
        result = run(
            _BuyOnceStrategy(),
            {"7203": tuple(days)},
            BacktestConfig(close_time=time(9, 15)),
        )
        assert result.halted_early
        # 8日ぶんのデータがあるが、3連敗した時点で止まる
        assert result.n_trades < 8

    def test_停止後は建てない(self) -> None:
        days: list[Bar] = []
        for d in range(1, 11):
            days.extend(_session(d, [1000.0] * 4))
        result = run(
            _BuyOnceStrategy(),
            {"7203": tuple(days)},
            BacktestConfig(close_time=time(9, 15)),
        )
        traded_days = {t.exit_time.day for t in result.trades}
        assert result.halted_early
        assert max(traded_days) < 10

    def test_勝ち続ければ停止しない(self) -> None:
        days: list[Bar] = []
        for d in range(1, 9):
            days.extend(_session(d, [1000.0, 1000.0, 1100.0, 1100.0]))
        result = run(
            _BuyOnceStrategy(),
            {"7203": tuple(days)},
            BacktestConfig(close_time=time(9, 15)),
        )
        assert not result.halted_early
        assert result.breaker_days == 0
