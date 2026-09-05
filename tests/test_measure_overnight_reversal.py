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
import json
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


class TestForwardReturns:
    """保有期間の軸。**将来の終値を使うが、これはシグナルではなく成績。**

    建てた後に確定する情報なので先読みではない（規約7）。
    """

    def _daily(self, mor: ModuleType, closes: list[float]) -> Any:
        """始値はすべて1000円、終値だけ動く日足。"""
        days = tuple(date(2026, 6, 1) + __import__("datetime").timedelta(days=i)
                     for i in range(len(closes)))
        return {
            "A": tuple(
                _bar("A", d, open_=1000.0, close=c)
                for d, c in zip(days, closes, strict=True)
            )
        }

    def test_保有期間ごとの将来リターンを持つ(self, mor: ModuleType) -> None:
        # index 2 が最初のペア。始値1000 に対し index 2/3/4 の終値
        daily = self._daily(mor, [1000, 1050, 1010, 1020, 1030, 1040, 1050])
        pair = mor.reversal_pairs(daily)[0]
        forward = dict(pair.forward_returns)
        assert forward[1] == pytest.approx(0.010)  # 1010
        assert forward[2] == pytest.approx(0.020)  # 1020
        assert forward[3] == pytest.approx(0.030)  # 1030
        assert forward[5] == pytest.approx(0.050)  # 1050

    def test_1日は当日のリターンと一致する(self, mor: ModuleType) -> None:
        daily = self._daily(mor, [1000, 1050, 1010, 1020])
        pair = mor.reversal_pairs(daily)[0]
        assert dict(pair.forward_returns)[1] == pytest.approx(
            pair.intraday_return_pct
        )

    def test_足りない日数は含まない(self, mor: ModuleType) -> None:
        """**銘柄の終端付近で無理に埋めない。**"""
        daily = self._daily(mor, [1000, 1050, 1010, 1020])
        pair = mor.reversal_pairs(daily)[0]
        assert set(dict(pair.forward_returns)) == {1, 2}

    def test_反転スコアは前日と逆方向で正(self, mor: ModuleType) -> None:
        # 前日 +5%（1000→1050）、その後じり下げ
        daily = self._daily(mor, [1000, 1050, 990, 980, 970, 960, 950])
        pair = mor.reversal_pairs(daily)[0]
        assert mor.reversal_score_at(pair, 1) == pytest.approx(0.010)
        assert mor.reversal_score_at(pair, 5) == pytest.approx(0.050)

    def test_該当しない保有期間はNone(self, mor: ModuleType) -> None:
        daily = self._daily(mor, [1000, 1050, 1010, 1020])
        pair = mor.reversal_pairs(daily)[0]
        assert mor.reversal_score_at(pair, 10) is None

    def test_前日動いていなければNone(self, mor: ModuleType) -> None:
        daily = self._daily(mor, [1000, 1000, 1010, 1020, 1030, 1040, 1050])
        pair = mor.reversal_pairs(daily)[0]
        assert mor.reversal_score_at(pair, 1) is None


class TestHoldingCost:
    def _pair(self, mor: ModuleType) -> Any:
        return mor.ReversalPair(
            symbol="A",
            day=DAYS[2],
            prior_move_pct=0.03,
            intraday_return_pct=-0.01,
            open_price=1000.0,
        )

    def test_当日決済なら金利ゼロ(self, mor: ModuleType) -> None:
        """**デイトレ信用は手数料0・金利0・貸株料0**（docs/02）。"""
        pair = self._pair(mor)
        assert mor.holding_cost_bps(pair, 1) == pytest.approx(
            mor.round_trip_cost_bps(pair)
        )

    def test_持ち越した夜数ぶんだけ金利が乗る(self, mor: ModuleType) -> None:
        pair = self._pair(mor)
        one = mor.holding_cost_bps(pair, 1, annual_rate=0.03)
        three = mor.holding_cost_bps(pair, 3, annual_rate=0.03)
        per_night = 0.03 / mor.TRADING_DAYS_PER_YEAR * 10_000.0
        assert three - one == pytest.approx(per_night * 2)

    def test_スプレッドは保有期間で増えない(self, mor: ModuleType) -> None:
        """**往復1回ぶん。** ここが「延ばせば損を薄められる」の中身。"""
        pair = self._pair(mor)
        zero_rate_1 = mor.holding_cost_bps(pair, 1, annual_rate=0.0)
        zero_rate_10 = mor.holding_cost_bps(pair, 10, annual_rate=0.0)
        assert zero_rate_1 == pytest.approx(zero_rate_10)

    def test_金利ゼロならスプレッドだけ(self, mor: ModuleType) -> None:
        pair = self._pair(mor)
        assert mor.holding_cost_bps(pair, 5, annual_rate=0.0) == pytest.approx(
            mor.round_trip_cost_bps(pair)
        )


class TestLoadCheapUniverse:
    """コストで切り出したユニバースの読み込み（意思決定ログ95）。

    **判定基準は変えない。** ユニバースを差し替えるだけで、
    `_report_verdict` の3条件はそのまま使う（意思決定ログ46・75）。
    """

    def test_universe_cheapを読む(
        self, mor: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mor, "DATA_ROOT", tmp_path)
        (tmp_path / "universe_cheap.json").write_text(
            json.dumps(
                {
                    "as_of": "2026-06-12",
                    "symbols": [
                        {"code": "1234", "name": "中型", "scale_category": "TOPIX Mid400"},
                        {"code": "5678", "name": "小型", "scale_category": "TOPIX Small 1"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = mor.load_symbols(topix100_only=False, cheap=True)
        assert [s.code for s in loaded] == ["1234", "5678"]
        # **TOPIX100 ではないので通常銘柄の呼値になる**
        assert not any(s.is_topix100 for s in loaded)

    def test_一覧が無ければ作り方を案内して終了(
        self, mor: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mor, "DATA_ROOT", tmp_path)
        with pytest.raises(SystemExit, match="measure_cost_landscape"):
            mor.load_symbols(topix100_only=False, cheap=True)

    def test_TOPIX100の一覧とは別物(
        self, mor: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**混ぜない。** 呼値が違うのでコストが1桁変わる。"""
        monkeypatch.setattr(mor, "DATA_ROOT", tmp_path)
        (tmp_path / "universe_cheap.json").write_text(
            json.dumps(
                {"symbols": [{"code": "1234", "name": "中型", "scale_category": "TOPIX Mid400"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (tmp_path / "master_scale_historical.json").write_text(
            json.dumps(
                {"symbols": [{"code": "7203", "name": "トヨタ", "scale_category": "TOPIX Core30"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        cheap = mor.load_symbols(topix100_only=False, cheap=True)
        topix = mor.load_symbols(topix100_only=True)
        assert [s.code for s in cheap] == ["1234"]
        assert [s.code for s in topix] == ["7203"]
