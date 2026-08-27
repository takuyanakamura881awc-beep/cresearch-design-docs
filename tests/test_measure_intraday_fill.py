"""scripts/measure_intraday_fill.py のテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で
直接読み込む（`tests/test_measure_gap_fade.py` と同じパターン）。

重点は4つ:

1. **日足と5分足を、両方そろっている日だけで突き合わせること。**
   片方しか無い日を無理に埋めると、測定そのものが嘘になる
2. **前日終値は5分足の有無と無関係に進むこと。**
   ここで飛ばすと、5分足の初日のギャップが「前日なし」になる
3. **14:50 以前の最後のバーで手仕舞うこと。** それ以降のバーを
   使ってしまうと、取りに行けない値動きを成績に入れることになる
4. **日クラスタで集計すること**（意思決定ログ72）
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from autotrader.types import Bar

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_intraday_fill.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_intraday_fill_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mif() -> ModuleType:
    return _load_script()


DAY1 = date(2026, 8, 3)
DAY2 = date(2026, 8, 4)


def _daily(symbol: str, day: date, *, open_: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(day.year, day.month, day.day),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=100_000,
    )


def _five_min(symbol: str, day: date, at: time, *, open_: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(day.year, day.month, day.day, at.hour, at.minute),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1_000,
    )


class TestIntradayPaths:
    def test_両方そろっている日だけを返す(self, mif: ModuleType) -> None:
        """**片方しか無い日を埋めない。** 5分足は58日ぶんしかない。"""
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1010),
                       _daily("A", DAY2, open_=1010, close=1020))}
        intraday = {"A": (_five_min("A", DAY2, time(9, 0), open_=1010, close=1012),)}
        paths = mif.intraday_paths(daily, intraday)
        assert [p.day for p in paths] == [DAY2]

    def test_前日終値は5分足が無い日も進む(self, mif: ModuleType) -> None:
        """**ここで飛ばすと、5分足の初日のギャップが「前日なし」になる。**"""
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1000),
                       _daily("A", DAY2, open_=950, close=980))}
        intraday = {"A": (_five_min("A", DAY2, time(9, 0), open_=950, close=955),)}
        paths = mif.intraday_paths(daily, intraday)
        assert paths[0].prev_close == pytest.approx(1000.0)
        assert paths[0].gap_pct == pytest.approx(-0.05)

    def test_区間が14時50分までに終わるバーで手仕舞う(self, mif: ModuleType) -> None:
        """**yfinance の5分足は区間の開始時刻でラベルされる。**

        14:50 のバーは 14:50〜14:55 を表すので、その終値は 14:55 の価格。
        `timestamp <= 14:50` で拾うと**5分ぶん先読みする**ので、
        使えるのは 14:45 開始のバーまで。
        """
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {
            "A": (
                _five_min("A", DAY1, time(9, 0), open_=1000, close=1005),
                _five_min("A", DAY1, time(14, 45), open_=1005, close=1020),
                _five_min("A", DAY1, time(14, 50), open_=1020, close=1030),
                _five_min("A", DAY1, time(14, 55), open_=1030, close=1050),
            )
        }
        paths = mif.intraday_paths(daily, intraday)
        assert paths[0].cutoff_close == pytest.approx(1020.0)
        assert paths[0].return_to_cutoff_bps == pytest.approx(200.0)
        assert paths[0].return_to_close_bps == pytest.approx(500.0)
        assert paths[0].tail_bps == pytest.approx(300.0)

    def test_14時50分開始のバーは使わない(self, mif: ModuleType) -> None:
        """**そのバーの終値は 14:55 の価格。** 使えば5分ぶん先読みになる。"""
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {"A": (_five_min("A", DAY1, time(14, 50), open_=1020, close=1030),)}
        assert mif.intraday_paths(daily, intraday) == ()

    def test_手仕舞えるバーが無い日は除く(self, mif: ModuleType) -> None:
        """手仕舞えないので測れない。**無理に埋めない。**"""
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {"A": (_five_min("A", DAY1, time(14, 55), open_=1030, close=1050),)}
        assert mif.intraday_paths(daily, intraday) == ()

    def test_最初と最後のバーの時刻を持つ(self, mif: ModuleType) -> None:
        """**始値がずれる原因の切り分けに要る。**"""
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {
            "A": (
                _five_min("A", DAY1, time(9, 5), open_=1002, close=1005),
                _five_min("A", DAY1, time(14, 45), open_=1005, close=1020),
                _five_min("A", DAY1, time(14, 55), open_=1030, close=1050),
            )
        }
        paths = mif.intraday_paths(daily, intraday)
        assert paths[0].first_bar_at == time(9, 5)
        # 最後のバーはカットオフを問わない（欠損の診断に使うため）
        assert paths[0].last_bar_at == time(14, 55)

    def test_TOPIX100かどうかを持つ(self, mif: ModuleType) -> None:
        """**net 正が出たのは TOPIX100 だけ。** 混ぜると比較にならない。"""
        daily = {
            "A": (_daily("A", DAY1, open_=1000, close=1050),),
            "B": (_daily("B", DAY1, open_=1000, close=1050),),
        }
        intraday = {
            "A": (_five_min("A", DAY1, time(9, 0), open_=1000, close=1005),),
            "B": (_five_min("B", DAY1, time(9, 0), open_=1000, close=1005),),
        }
        paths = mif.intraday_paths(daily, intraday, topix100_codes=frozenset({"A"}))
        flags = {p.symbol: p.topix100 for p in paths}
        assert flags == {"A": True, "B": False}

    def test_バーの順序が乱れていても正しく拾う(self, mif: ModuleType) -> None:
        """保存順に依存しない。"""
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {
            "A": (
                _five_min("A", DAY1, time(14, 45), open_=1005, close=1020),
                _five_min("A", DAY1, time(9, 0), open_=1000, close=1005),
            )
        }
        paths = mif.intraday_paths(daily, intraday)
        assert paths[0].first_bar_open == pytest.approx(1000.0)
        assert paths[0].cutoff_close == pytest.approx(1020.0)

    def test_5分足が無い銘柄は飛ばす(self, mif: ModuleType) -> None:
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        assert mif.intraday_paths(daily, {}) == ()

    def test_銘柄の初日はギャップを持たない(self, mif: ModuleType) -> None:
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {"A": (_five_min("A", DAY1, time(9, 0), open_=1000, close=1005),)}
        paths = mif.intraday_paths(daily, intraday)
        assert paths[0].gap_pct is None
        assert paths[0].fade_bps(to_cutoff=True) is None


class TestOpenMismatch:
    def test_一致していればゼロ(self, mif: ModuleType) -> None:
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {"A": (_five_min("A", DAY1, time(9, 0), open_=1000, close=1005),)}
        assert mif.intraday_paths(daily, intraday)[0].open_mismatch_bps == pytest.approx(0.0)

    def test_ずれていればbpsで出る(self, mif: ModuleType) -> None:
        """**ずれていれば、これまでの gap_pct の計算そのものが疑わしい。**"""
        daily = {"A": (_daily("A", DAY1, open_=1000, close=1050),)}
        intraday = {"A": (_five_min("A", DAY1, time(9, 0), open_=1001, close=1005),)}
        assert mif.intraday_paths(daily, intraday)[0].open_mismatch_bps == pytest.approx(10.0)


class TestFadeBps:
    def _path(self, mif: ModuleType, *, gap_down: bool) -> Any:
        prev, opened = (1000.0, 950.0) if gap_down else (1000.0, 1050.0)
        daily = {"A": (_daily("A", DAY1, open_=prev, close=prev),
                       _daily("A", DAY2, open_=opened, close=opened * 1.01))}
        intraday = {
            "A": (
                _five_min("A", DAY2, time(9, 0), open_=opened, close=opened),
                _five_min("A", DAY2, time(14, 45), open_=opened, close=opened * 1.005),
            )
        }
        return mif.intraday_paths(daily, intraday)[0]

    def test_ギャップダウン後に上げればフェードで正(self, mif: ModuleType) -> None:
        path = self._path(mif, gap_down=True)
        assert path.fade_bps(to_cutoff=True) == pytest.approx(50.0)
        assert path.fade_bps(to_cutoff=False) == pytest.approx(100.0)

    def test_ギャップアップ後に上げればフェードで負(self, mif: ModuleType) -> None:
        path = self._path(mif, gap_down=False)
        assert path.fade_bps(to_cutoff=True) == pytest.approx(-50.0)
        assert path.fade_bps(to_cutoff=False) == pytest.approx(-100.0)


class TestClusteredMean:
    def test_日ごとに等ウェイトにする(self, mif: ModuleType) -> None:
        """**該当銘柄が多い日を過大に扱わない**（意思決定ログ72）。"""
        samples = tuple([(DAY1, 100.0)] * 9 + [(DAY2, -100.0)])
        assert mif.clustered_mean(samples).mean_bps == pytest.approx(0.0)
        assert mif.clustered_mean(samples).days == 2

    def test_件数を増やしても日数が同じならt値は変わらない(self, mif: ModuleType) -> None:
        small = tuple([(DAY1, 20.0)] * 3 + [(DAY2, 10.0)] * 3)
        large = tuple([(DAY1, 20.0)] * 30 + [(DAY2, 10.0)] * 30)
        assert mif.clustered_mean(small).t_stat == pytest.approx(
            mif.clustered_mean(large).t_stat
        )

    def test_ばらつきがなければt値ゼロ(self, mif: ModuleType) -> None:
        samples = ((DAY1, 10.0), (DAY2, 10.0))
        assert mif.clustered_mean(samples).t_stat == 0.0

    def test_該当日が2日未満ならNone(self, mif: ModuleType) -> None:
        assert mif.clustered_mean(((DAY1, 10.0),)) is None
        assert mif.clustered_mean(()) is None
