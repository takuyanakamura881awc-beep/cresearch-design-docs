"""scripts/measure_overnight_reversal.py のテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で
直接読み込む（`tests/test_measure_gap_fade.py` と同じパターン）。

重点は4つ:

1. **シグナルが前日大引けまでの情報だけで決まること。**
   ここが崩れると寄成注文に間に合わず、ギャップ・フェードと同じ理由で死ぬ
   （意思決定ログ86）
2. **3日ぶんの日足が要る**（前々日終値・前日終値・当日）ので、
   各銘柄の最初の2日を除外すること
3. `reversal_score` の符号が反転方向で正になること
4. **判定に使うのは日クラスタの統計**であること（意思決定ログ72）
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from autotrader.tick import spread_yen
from autotrader.types import Bar

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "measure_overnight_reversal.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "measure_overnight_reversal_script", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mor() -> ModuleType:
    return _load_script()


DAYS = tuple(date(2026, 6, d) for d in (1, 2, 3, 4, 5))


def _bar(symbol: str, day: date, *, open_: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(day.year, day.month, day.day),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=100_000,
    )


class TestReversalPairs:
    def test_前々日終値と前日終値からシグナルを作る(self, mor: ModuleType) -> None:
        """**当日のバーを一切参照しない。** これが寄成に間に合う条件。"""
        daily = {
            "A": (
                _bar("A", DAYS[0], open_=1000, close=1000),
                _bar("A", DAYS[1], open_=1000, close=1050),  # 前日 +5%
                _bar("A", DAYS[2], open_=1040, close=1030),
            )
        }
        pairs = mor.reversal_pairs(daily)
        assert len(pairs) == 1
        assert pairs[0].day == DAYS[2]
        assert pairs[0].prior_move_pct == pytest.approx(0.05)

    def test_当日は始値と終値だけを使う(self, mor: ModuleType) -> None:
        """建てた後・手仕舞う時点の情報しか使わない（規約7）。"""
        daily = {
            "A": (
                _bar("A", DAYS[0], open_=1000, close=1000),
                _bar("A", DAYS[1], open_=1000, close=1050),
                _bar("A", DAYS[2], open_=1040, close=1030),
            )
        }
        pairs = mor.reversal_pairs(daily)
        assert pairs[0].open_price == pytest.approx(1040.0)
        # (1030 - 1040) / 1040
        assert pairs[0].intraday_return_pct == pytest.approx(-10 / 1040)

    def test_各銘柄の最初の2日は除外する(self, mor: ModuleType) -> None:
        """**3日ぶんの日足が要る。** 前々日終値が無ければシグナルが作れない。"""
        daily = {
            "A": tuple(
                _bar("A", d, open_=1000, close=1000 + i * 10)
                for i, d in enumerate(DAYS)
            )
        }
        pairs = mor.reversal_pairs(daily)
        assert [p.day for p in pairs] == list(DAYS[2:])

    def test_日足が2日以下なら何も返さない(self, mor: ModuleType) -> None:
        daily = {
            "A": (
                _bar("A", DAYS[0], open_=1000, close=1000),
                _bar("A", DAYS[1], open_=1000, close=1050),
            )
        }
        assert mor.reversal_pairs(daily) == ()

    def test_価格が0以下の日は除外する(self, mor: ModuleType) -> None:
        daily = {
            "A": (
                _bar("A", DAYS[0], open_=1000, close=0.0),
                _bar("A", DAYS[1], open_=1000, close=1050),
                _bar("A", DAYS[2], open_=1040, close=1030),
            )
        }
        assert mor.reversal_pairs(daily) == ()

    def test_バーの順序が乱れていても正しく組む(self, mor: ModuleType) -> None:
        daily = {
            "A": (
                _bar("A", DAYS[2], open_=1040, close=1030),
                _bar("A", DAYS[0], open_=1000, close=1000),
                _bar("A", DAYS[1], open_=1000, close=1050),
            )
        }
        assert mor.reversal_pairs(daily)[0].prior_move_pct == pytest.approx(0.05)

    def test_TOPIX100かどうかを持つ(self, mor: ModuleType) -> None:
        daily = {
            code: (
                _bar(code, DAYS[0], open_=1000, close=1000),
                _bar(code, DAYS[1], open_=1000, close=1050),
                _bar(code, DAYS[2], open_=1040, close=1030),
            )
            for code in ("A", "B")
        }
        pairs = mor.reversal_pairs(daily, frozenset({"A"}))
        assert {p.symbol: p.topix100 for p in pairs} == {"A": True, "B": False}


class TestReversalScore:
    def _pair(self, mor: ModuleType, *, prior: float, move: float) -> Any:
        return mor.ReversalPair(
            symbol="A",
            day=DAYS[2],
            prior_move_pct=prior,
            intraday_return_pct=move,
            open_price=1000.0,
        )

    def test_前日上げて当日下げれば正(self, mor: ModuleType) -> None:
        assert mor.reversal_score(
            self._pair(mor, prior=0.05, move=-0.01)
        ) == pytest.approx(0.01)

    def test_前日上げて当日も上げれば負(self, mor: ModuleType) -> None:
        """継続（反転しなかった）。"""
        assert mor.reversal_score(
            self._pair(mor, prior=0.05, move=0.01)
        ) == pytest.approx(-0.01)

    def test_前日下げて当日上げれば正(self, mor: ModuleType) -> None:
        assert mor.reversal_score(
            self._pair(mor, prior=-0.05, move=0.01)
        ) == pytest.approx(0.01)

    def test_前日動いていなければゼロ(self, mor: ModuleType) -> None:
        assert mor.reversal_score(self._pair(mor, prior=0.0, move=0.01)) == 0.0


class TestBucketStats:
    def _pairs(self, mor: ModuleType) -> tuple[Any, ...]:
        # 前日の値動きが 1%/2%/3%/4% の4件（別々の日）。当日はすべて逆方向1%
        return tuple(
            mor.ReversalPair(
                symbol=f"S{i}",
                day=DAYS[i % len(DAYS)],
                prior_move_pct=prior,
                intraday_return_pct=-0.01,
                open_price=1000.0,
            )
            for i, prior in enumerate((0.01, 0.02, 0.03, 0.04))
        )

    def test_閾値で絞り込む(self, mor: ModuleType) -> None:
        pairs = self._pairs(mor)
        assert mor.bucket_stats(pairs, 0.0).n == 4
        assert mor.bucket_stats(pairs, 0.025).n == 2

    def test_該当が2件未満ならNone(self, mor: ModuleType) -> None:
        assert mor.bucket_stats(self._pairs(mor), 0.99) is None

    def test_netはgrossからコストを引いた値(self, mor: ModuleType) -> None:
        stats = mor.bucket_stats(self._pairs(mor), 0.0)
        assert stats.net_bps == pytest.approx(stats.gross_bps - stats.cost_bps)
        assert stats.cost_bps > 0

    def test_コストはtickモジュールと同じ値(self, mor: ModuleType) -> None:
        """**コストモデルを診断ごとに作り直していない**ことの確認。"""
        stats = mor.bucket_stats(self._pairs(mor), 0.0)
        assert stats.cost_bps == pytest.approx(
            float(spread_yen(1000.0)) / 1000.0 * 10_000.0
        )

    def test_日クラスタの統計を持つ(self, mor: ModuleType) -> None:
        """**判定に使うのはこちら**（意思決定ログ72）。"""
        stats = mor.bucket_stats(self._pairs(mor), 0.0)
        assert stats.clustered is not None
        assert stats.clustered.days == 4
        assert stats.clustered_net_bps == pytest.approx(
            stats.clustered.mean_bps - stats.cost_bps
        )

    def test_全件が同じ日なら日クラスタは出ない(self, mor: ModuleType) -> None:
        """**独立な観測が1日ぶんしかない**ので t値を出さない。"""
        pairs = tuple(
            mor.ReversalPair(
                symbol=f"S{i}",
                day=DAYS[0],
                prior_move_pct=0.03,
                intraday_return_pct=-0.01,
                open_price=1000.0,
            )
            for i in range(20)
        )
        stats = mor.bucket_stats(pairs, 0.0)
        assert stats.n == 20
        assert stats.clustered is None
        assert stats.clustered_net_bps is None
