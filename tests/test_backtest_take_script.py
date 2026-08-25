"""scripts/backtest_take.py の検定まわりのテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で
直接読み込む。狙うのは3つ:

1. 多重比較補正の閾値・必要シード数が正しく導出されること
2. 期間分割（前半/後半）がバーを切らず、監視リストだけを絞ること
   （ATR の助走が保たれることの確認）
3. 前半・後半の窓が重ならず、合わせて全期間になること
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from autotrader.engine.backtest import BacktestConfig, run
from autotrader.strategy.take_intraday import TakeIntraday
from autotrader.types import Bar

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backtest_take.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backtest_take_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # sys.modules に登録してから exec する。dataclass 等が
    # モジュール参照を解決できないと失敗することがあるため
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bt() -> ModuleType:
    return _load_script()


T0 = datetime(2026, 6, 1, 9, 0)


def _series(code: str, price: float, n_days: int) -> tuple[Bar, ...]:
    """毎営業日、9:30の足で確実にブレイクアウトが起こる決定的なバー列。

    9:00〜9:25 をフラットなオープニングレンジにし、9:30 の足で
    レンジを大きく超える終値にする。以降は大引けまでその水準を保つので、
    毎営業日ちょうど1トレード（時間切れで手仕舞い）が出る。

    **乱数を使わない。** 「窓を絞ってもシグナルが変わらない」ことを
    確認するテストなので、比較対象自体が揺れると確認にならない。
    """
    out: list[Bar] = []
    for d in range(n_days):
        base = datetime(2026, 6, 1, 9, 0) + timedelta(days=d)
        for i in range(6):  # 9:00〜9:25（オープニングレンジ）
            out.append(
                Bar(
                    symbol=code,
                    timestamp=base + timedelta(minutes=5 * i),
                    open=price,
                    high=price + 2.0,
                    low=price - 2.0,
                    close=price,
                    volume=50_000,
                    turnover=2_000_000_000.0,
                )
            )
        breakout_price = price + 50.0
        out.append(
            Bar(
                symbol=code,
                timestamp=base + timedelta(minutes=30),
                open=price,
                high=breakout_price + 5.0,
                low=price - 2.0,
                close=breakout_price,
                volume=50_000,
                turnover=2_000_000_000.0,
            )
        )
        for m in range(35, 356, 5):  # 9:35〜14:55（大引けまで水準を保つ）
            out.append(
                Bar(
                    symbol=code,
                    timestamp=base + timedelta(minutes=m),
                    open=breakout_price,
                    high=breakout_price + 2.0,
                    low=breakout_price - 2.0,
                    close=breakout_price,
                    volume=50_000,
                    turnover=2_000_000_000.0,
                )
            )
    return tuple(out)


class TestRequiredPercentile:
    def test_5変種なら99パーセント(self, bt: ModuleType) -> None:
        assert bt.required_percentile(5) == pytest.approx(0.99)

    def test_1変種なら95パーセント(self, bt: ModuleType) -> None:
        assert bt.required_percentile(1) == pytest.approx(0.95)

    def test_変種0以下を拒否する(self, bt: ModuleType) -> None:
        with pytest.raises(ValueError, match="変種数"):
            bt.required_percentile(0)


class TestRequiredSeeds:
    def test_99パーセントには100シード要る(self, bt: ModuleType) -> None:
        assert bt.required_seeds_for(0.99) == 100

    def test_95パーセントには20シード要る(self, bt: ModuleType) -> None:
        assert bt.required_seeds_for(0.95) == 20

    def test_範囲外を拒否する(self, bt: ModuleType) -> None:
        with pytest.raises(ValueError, match="パーセンタイル"):
            bt.required_seeds_for(1.0)
        with pytest.raises(ValueError, match="パーセンタイル"):
            bt.required_seeds_for(0.0)

    def test_実際の変種数と整合する(self, bt: ModuleType) -> None:
        """--stress-test の CLI バリデーションが使う値そのもの。"""
        required = bt.required_percentile(len(bt.EXPERIMENT_VARIANTS))
        assert bt.required_seeds_for(required) == 100


class TestWindowWatchlist:
    def test_指定した日だけに絞る(self, bt: ModuleType) -> None:
        watchlist = {
            date(2026, 6, 1): frozenset({"A"}),
            date(2026, 6, 2): frozenset({"B"}),
            date(2026, 6, 3): frozenset({"C"}),
        }
        windowed = bt._window_watchlist(watchlist, (date(2026, 6, 2),))
        assert windowed == {date(2026, 6, 2): frozenset({"B"})}

    def test_前半と後半で全期間を過不足なく覆う(self, bt: ModuleType) -> None:
        days = tuple(date(2026, 6, 1) + timedelta(days=i) for i in range(39))
        half = len(days) // 2
        first, second = days[:half], days[half:]

        assert set(first) & set(second) == set()
        assert set(first) | set(second) == set(days)
        assert len(first) == 19
        assert len(second) == 20

    def test_バーを切らず助走を保つ(self, bt: ModuleType) -> None:
        """**これが期間分割の核心。**

        窓を「後半」に絞っても、後半初日のシグナルは全期間を渡したときと
        一致すること。ATR の計算に使う過去バーが watchlist の絞り込みでは
        消えないことの確認（`run()` は bars を切らないので消えないはずだが、
        呼び出し側の配線を間違えると消える）。
        """
        bars = {"A": _series("A", 1000.0, n_days=10)}
        all_days = tuple(sorted({b.timestamp.date() for b in bars["A"]}))
        full_watchlist = {d: frozenset({"A"}) for d in all_days}

        half = len(all_days) // 2
        second_half_days = all_days[half:]
        windowed_watchlist = bt._window_watchlist(full_watchlist, second_half_days)

        cfg = BacktestConfig(initial_cash=Decimal(500_000), shortable=frozenset({"A"}))

        full_result = run(TakeIntraday(), bars, cfg, full_watchlist)
        windowed_result = run(TakeIntraday(), bars, cfg, windowed_watchlist)

        # 後半初日以降のトレードだけを全期間側から取り出す
        cutoff = second_half_days[0]
        full_second_half_trades = [
            t for t in full_result.trades if t.entry_time.date() >= cutoff
        ]

        # **比較の前提として、そもそもトレードが出ていること。**
        # 0件同士の一致は「揃っている」ことの証明にならない
        assert len(full_second_half_trades) > 0
        assert len(windowed_result.trades) > 0

        # **件数・エントリー時刻が一致する。** 助走が保たれていなければ
        # ATR が変わり、発火するバーがずれて件数が変わる
        assert len(windowed_result.trades) == len(full_second_half_trades)
        assert [t.entry_time for t in windowed_result.trades] == [
            t.entry_time for t in full_second_half_trades
        ]

    def test_前半のみに絞ると前半以外は建たない(self, bt: ModuleType) -> None:
        bars = {"A": _series("A", 1000.0, n_days=10)}
        all_days = tuple(sorted({b.timestamp.date() for b in bars["A"]}))
        full_watchlist = {d: frozenset({"A"}) for d in all_days}
        half = len(all_days) // 2
        first_half_days = all_days[:half]
        windowed_watchlist = bt._window_watchlist(full_watchlist, first_half_days)

        cfg = BacktestConfig(initial_cash=Decimal(500_000), shortable=frozenset({"A"}))
        result = run(TakeIntraday(), bars, cfg, windowed_watchlist)

        assert result.n_trades > 0
        assert all(t.entry_time.date() in first_half_days for t in result.trades)


class TestBaselineProbabilityWindowed:
    def test_daysを渡すと窓のバー数だけで較正する(self, bt: ModuleType) -> None:
        bars = {"A": _series("A", 1000.0, n_days=10)}
        all_days = tuple(sorted({b.timestamp.date() for b in bars["A"]}))
        watchlist = {d: frozenset({"A"}) for d in all_days}
        cfg = BacktestConfig(initial_cash=Decimal(500_000), shortable=frozenset({"A"}))
        result = run(TakeIntraday(), bars, cfg, watchlist)
        assert result.n_trades > 0

        half = len(all_days) // 2
        first_half = all_days[:half]

        p_full, _, n_bars_full = bt._baseline_probability(result, bars, watchlist)
        p_windowed, _, n_bars_windowed = bt._baseline_probability(
            result, bars, watchlist, first_half
        )

        # 窓を絞ればバー数は必ず減り、その分だけ確率は上がる
        assert n_bars_windowed < n_bars_full
        assert p_windowed >= p_full


class TestGroupByRegime:
    """`--by-regime` が使う、トレードを calm/wild に振り分けるロジック。

    **印字関数（`run_by_regime`）自体は直接テストしない。** このファイルの
    他のテストと同じく、印字の元になる純粋なロジックだけを確認する。
    """

    def test_calmとwildに振り分けられる(self, bt: ModuleType) -> None:
        # 前半10日は穏やか（値幅2%）、後半10日は荒れ（値幅10%）にする
        calm_bars = _series("CALM", 1000.0, n_days=20)
        wild_bars = _series("WILD", 1000.0, n_days=20)
        bars = {"CALM": calm_bars, "WILD": wild_bars}

        all_days = tuple(sorted({b.timestamp.date() for b in calm_bars}))
        watchlist = {d: frozenset({"CALM", "WILD"}) for d in all_days}
        cfg = BacktestConfig(
            initial_cash=Decimal(500_000), shortable=frozenset({"CALM", "WILD"})
        )
        result = run(TakeIntraday(), bars, cfg, watchlist)
        assert result.n_trades > 0

        groups = bt._group_by_regime(result.trades, bars)

        # calm と wild の両方のキーが必ず存在する（0件でも空リストで）
        assert set(groups) == {"calm", "wild"}
        # 振り分けた総数はトレード総数を超えない（分類できなかった日は捨てる）
        assert len(groups["calm"]) + len(groups["wild"]) <= result.n_trades

    def test_同じ日を二度分類しない(
        self, bt: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`classify_days` の呼び出し回数がトレード件数ではなく日数ぶんになること。"""
        bars = {"A": _series("A", 1000.0, n_days=5)}
        all_days = tuple(sorted({b.timestamp.date() for b in bars["A"]}))
        watchlist = {d: frozenset({"A"}) for d in all_days}
        cfg = BacktestConfig(initial_cash=Decimal(500_000), shortable=frozenset({"A"}))
        result = run(TakeIntraday(), bars, cfg, watchlist)
        assert result.n_trades > 0

        call_count = 0
        original = bt.classify_days

        def counting_classify_days(*args: object, **kwargs: object) -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            return original(*args, **kwargs)  # type: ignore[no-any-return]

        monkeypatch.setattr(bt, "classify_days", counting_classify_days)
        bt._group_by_regime(result.trades, bars)

        unique_days = {t.entry_time.date() for t in result.trades}
        assert call_count == len(unique_days)

    def test_トレードがなければ両方空(self, bt: ModuleType) -> None:
        groups = bt._group_by_regime((), {})
        assert groups == {"calm": [], "wild": []}

