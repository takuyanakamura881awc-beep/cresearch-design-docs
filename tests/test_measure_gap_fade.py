"""scripts/measure_gap_fade.py のテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で
直接読み込む（`tests/test_backtest_take_script.py` と同じパターン）。

重点は5つ:

1. `gap_pct` / `intraday_return_pct` が正しい式で計算されること
2. 銘柄の初日（前日終値がない）を除外すること
3. `fade_score` の符号がフェード方向で正、ギャップ&ゴー方向で負になること
4. **往復コストが `autotrader.tick` と同じ値になること**（診断ごとに
   コストモデルを作り直していないことの確認）
5. **`net_bps` が gross からコストを引いた値であること**——ここを
   取り違えると「コスト後に残る」という誤った結論を出しかねない
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

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_gap_fade.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_gap_fade_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gf() -> ModuleType:
    return _load_script()


def _daily_bar(symbol: str, day: date, *, open_: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(day.year, day.month, day.day, 0, 0),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=10_000,
    )


DAY1 = date(2026, 6, 1)
DAY2 = date(2026, 6, 2)
DAY3 = date(2026, 6, 3)


class TestGapFadePairs:
    def test_gap_pctとintraday_return_pctを正しく計算する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                # 前日終値1000から1050で寄り付き（gap +5%）、990で引け
                _daily_bar("A", DAY2, open_=1050.0, close=990.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.symbol == "A"
        assert pair.gap_pct == pytest.approx((1050.0 - 1000.0) / 1000.0)
        assert pair.intraday_return_pct == pytest.approx((990.0 - 1050.0) / 1050.0)

    def test_銘柄の初日は除外する(self, gf: ModuleType) -> None:
        """前日終値がない最初の日はギャップを定義できない。"""
        daily_bars = {"A": (_daily_bar("A", DAY1, open_=1000.0, close=1010.0),)}
        assert gf.gap_fade_pairs(daily_bars) == ()

    def test_前日終値が0以下の日は除外する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=0.0),
                _daily_bar("A", DAY2, open_=1000.0, close=1000.0),
            ),
        }
        assert gf.gap_fade_pairs(daily_bars) == ()

    def test_当日始値が0以下の日は除外する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                _daily_bar("A", DAY2, open_=0.0, close=1000.0),
            ),
        }
        assert gf.gap_fade_pairs(daily_bars) == ()

    def test_複数銘柄複数日で銘柄ごとに独立して計算する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                _daily_bar("A", DAY2, open_=1010.0, close=1005.0),
                _daily_bar("A", DAY3, open_=1020.0, close=1015.0),
            ),
            "B": (
                _daily_bar("B", DAY1, open_=500.0, close=500.0),
                _daily_bar("B", DAY2, open_=490.0, close=495.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        # Aは2日ぶん（DAY2, DAY3）、Bは1日ぶん（DAY2）
        assert sum(1 for p in pairs if p.symbol == "A") == 2
        assert sum(1 for p in pairs if p.symbol == "B") == 1

    def test_バーの並び順に依存しない(self, gf: ModuleType) -> None:
        """`BarStore.read` の返す順が保証されなくても、時刻で並べ替えて計算する。"""
        daily_bars = {
            "A": (
                _daily_bar("A", DAY2, open_=1050.0, close=990.0),
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        assert len(pairs) == 1
        assert pairs[0].gap_pct == pytest.approx(0.05)


class TestFadeScore:
    def test_ギャップアップして戻れば正(self, gf: ModuleType) -> None:
        """フェード（ギャップ方向と逆に動いた）は正のスコア。"""
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", day=DAY1, gap_pct=0.02, intraday_return_pct=-0.01
        )
        assert gf.fade_score(pair) == pytest.approx(0.01)

    def test_ギャップアップしてさらに伸びれば負(self, gf: ModuleType) -> None:
        """ギャップ&ゴー（ギャップ方向にさらに伸びた）は負のスコア。"""
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", day=DAY1, gap_pct=0.02, intraday_return_pct=0.01
        )
        assert gf.fade_score(pair) == pytest.approx(-0.01)

    def test_ギャップダウンして戻れば正(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", day=DAY1, gap_pct=-0.02, intraday_return_pct=0.01
        )
        assert gf.fade_score(pair) == pytest.approx(0.01)

    def test_ギャップダウンしてさらに下げれば負(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", day=DAY1, gap_pct=-0.02, intraday_return_pct=-0.01
        )
        assert gf.fade_score(pair) == pytest.approx(-0.01)

    def test_ギャップがゼロなら符号を持たずゼロ(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", day=DAY1, gap_pct=0.0, intraday_return_pct=0.01
        )
        assert gf.fade_score(pair) == 0.0


class TestOpenPrice:
    def test_当日始値を保持する(self, gf: ModuleType) -> None:
        """コストは株価で決まるので、始値を持っていないと見積れない。"""
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                _daily_bar("A", DAY2, open_=1050.0, close=990.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        assert pairs[0].open_price == pytest.approx(1050.0)


class TestRoundTripCostBps:
    def test_tickモジュールと同じ値になる(self, gf: ModuleType) -> None:
        """**コストモデルを診断ごとに作り直していない**ことの確認。"""
        price = 1000.0
        pair = gf.GapFadePair(
            open_price=price, symbol="A", day=DAY1, gap_pct=0.02, intraday_return_pct=-0.01
        )
        expected = float(spread_yen(price)) / price * 10_000.0
        assert gf.round_trip_cost_bps(pair) == pytest.approx(expected)

    def test_安い株ほど往復コストが高い(self, gf: ModuleType) -> None:
        """呼値は絶対額なので、株価が低いほど比率としては重くなる。"""
        cheap = gf.GapFadePair(
            open_price=500.0, symbol="A", day=DAY1, gap_pct=0.02, intraday_return_pct=-0.01
        )
        pricey = gf.GapFadePair(
            open_price=2500.0, symbol="B", day=DAY1, gap_pct=0.02, intraday_return_pct=-0.01
        )
        assert gf.round_trip_cost_bps(cheap) > gf.round_trip_cost_bps(pricey)


class TestBucketStats:
    def _pairs(self, gf: ModuleType) -> tuple[object, ...]:
        # |gap| が 1%/2%/3% の3件。intraday はすべてギャップと逆方向1%（フェード）
        return tuple(
            gf.GapFadePair(
                open_price=1000.0,
                symbol=f"S{i}",
                day=DAY1,
                gap_pct=gap,
                intraday_return_pct=-0.01,
            )
            for i, gap in enumerate((0.01, 0.02, 0.03))
        )

    def test_閾値で絞り込む(self, gf: ModuleType) -> None:
        pairs = self._pairs(gf)
        assert gf.bucket_stats(pairs, 0.0).n == 3
        assert gf.bucket_stats(pairs, 0.015).n == 2

    def test_該当が2件未満ならNone(self, gf: ModuleType) -> None:
        """標準偏差が計算できないので、無理に数字を出さない。"""
        pairs = self._pairs(gf)
        assert gf.bucket_stats(pairs, 0.025) is None
        assert gf.bucket_stats(pairs, 0.99) is None

    def test_gross_bpsはfade_scoreの平均をbpsにしたもの(self, gf: ModuleType) -> None:
        pairs = self._pairs(gf)
        stats = gf.bucket_stats(pairs, 0.0)
        # 全件が「ギャップと逆に1%」＝ fade_score +0.01 = +100bps
        assert stats.gross_bps == pytest.approx(100.0)

    def test_netはgrossからコストを引いた値(self, gf: ModuleType) -> None:
        """**取り違えると「コスト後に残る」という誤った結論になる。**"""
        pairs = self._pairs(gf)
        stats = gf.bucket_stats(pairs, 0.0)
        assert stats.net_bps == pytest.approx(stats.gross_bps - stats.cost_bps)
        assert stats.cost_bps > 0

    def test_ばらつきがなければt値は無限大にせずstderrゼロで0を返す(
        self, gf: ModuleType
    ) -> None:
        """全件が同じ値だと標準誤差が0になる。ゼロ除算を外に漏らさない。"""
        pairs = self._pairs(gf)
        stats = gf.bucket_stats(pairs, 0.0)
        assert stats.stderr_bps == pytest.approx(0.0)
        assert stats.t_stat == 0.0


class TestDay:
    def test_当日の日付を保持する(self, gf: ModuleType) -> None:
        """**同じ日のシグナルをまとめる**ために要る（枠が埋まるかの判定）。"""
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                _daily_bar("A", DAY2, open_=1050.0, close=990.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        # ペアは「前日終値 → 当日」なので、日付は**当日**（DAY2）
        assert pairs[0].day == DAY2


class TestTradeSide:
    def test_ギャップアップのフェードは売建(self, gf: ModuleType) -> None:
        """**安全装置#3（ストップ必須）が適用される側。**"""
        pair = gf.GapFadePair(
            symbol="A", day=DAY1, gap_pct=0.02, intraday_return_pct=-0.01, open_price=1000.0
        )
        assert gf.trade_side(pair) == "売建"

    def test_ギャップダウンのフェードは買建(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(
            symbol="A", day=DAY1, gap_pct=-0.02, intraday_return_pct=0.01, open_price=1000.0
        )
        assert gf.trade_side(pair) == "買建"


class TestCapacityStats:
    """建玉シミュレーションが安全装置をバイパスしていないことを固定する。

    **ここが緩むと月利が過大に出る。** 1銘柄25%・同時5銘柄・レバ1倍は
    どれも建てられる量を減らす制約なので、外れると成績が良い側にずれる。
    """

    def _pair(
        self,
        gf: ModuleType,
        symbol: str,
        day: date,
        *,
        gap: float,
        move: float,
        price: float = 1000.0,
    ) -> object:
        return gf.GapFadePair(
            symbol=symbol,
            day=day,
            gap_pct=gap,
            intraday_return_pct=move,
            open_price=price,
        )

    def test_同時保有は5銘柄を超えない(self, gf: ModuleType) -> None:
        """同じ日に10銘柄シグナルが出ても、埋まる枠は5（安全装置#7）。

        資金50万円・株価1,000円なら1単元＝10万円＝資金の20%。5枠で
        ちょうど100%になるので、**枠の上限だけが効く**状況を作れる。
        """
        pairs = tuple(self._pair(gf, f"S{i}", DAY1, gap=0.03, move=-0.01) for i in range(10))
        stats = gf.capacity_stats(pairs, 500_000, threshold=0.02)
        assert stats.mean_slots_filled == pytest.approx(5.0)
        assert stats.mean_deployed_pct == pytest.approx(100.0)

    def test_建玉総額は現金を超えない(self, gf: ModuleType) -> None:
        """レバレッジ1倍の不変条件。5枠 × 25% = 125% になってはいけない。

        1銘柄上限(25%)だけを守って5枠埋めると125%になる。現金でも
        止めているので、**枠は4つで打ち止めになるのが正しい**。
        """
        pairs = tuple(self._pair(gf, f"S{i}", DAY1, gap=0.03, move=-0.01) for i in range(10))
        stats = gf.capacity_stats(pairs, 10_000_000, threshold=0.02)
        assert stats.mean_deployed_pct == pytest.approx(100.0)
        assert stats.mean_slots_filled == pytest.approx(4.0)

    def test_1銘柄の建玉は資金の四分の一を超えない(self, gf: ModuleType) -> None:
        """安全装置#7。1銘柄しかシグナルがなくても資金を全部は入れない。"""
        pairs = (self._pair(gf, "A", DAY1, gap=0.03, move=-0.01, price=1000.0),)
        stats = gf.capacity_stats(pairs, 1_000_000, threshold=0.02)
        # 100万円の25% = 25万円 → 1000円 × 100株 = 10万円 なので2単元まで
        assert stats.mean_deployed_pct == pytest.approx(20.0)

    def test_1単元も買えない株価の銘柄は使われない(self, gf: ModuleType) -> None:
        """株価上限は資金 × 25% ÷ 100株。**これが TOPIX100 を阻んだ制約。**"""
        pairs = (self._pair(gf, "A", DAY1, gap=0.03, move=-0.01, price=2_000.0),)
        # 資金50万円 → 25% = 12.5万円 < 2,000円 × 100株 = 20万円
        stats = gf.capacity_stats(pairs, 500_000, threshold=0.02)
        assert stats.symbols_used == 0
        assert stats.mean_slots_filled == pytest.approx(0.0)

    def test_資金を増やすと使える銘柄が増える(self, gf: ModuleType) -> None:
        pairs = (
            self._pair(gf, "CHEAP", DAY1, gap=0.03, move=-0.01, price=1_000.0),
            self._pair(gf, "PRICEY", DAY1, gap=0.03, move=-0.01, price=3_000.0),
        )
        assert gf.capacity_stats(pairs, 500_000, threshold=0.02).symbols_used == 1
        assert gf.capacity_stats(pairs, 2_000_000, threshold=0.02).symbols_used == 2

    def test_シグナルが出ない日も分母に入る(self, gf: ModuleType) -> None:
        """**ここを落とすと月利が跳ね上がる。** 建てなかった日はゼロ%の日。"""
        pairs = (
            self._pair(gf, "A", DAY1, gap=0.03, move=-0.01),
            self._pair(gf, "A", DAY2, gap=0.001, move=-0.01),  # 閾値未満
        )
        stats = gf.capacity_stats(pairs, 1_000_000, threshold=0.02)
        assert stats.days == 2
        assert stats.mean_slots_filled == pytest.approx(0.5)

    def test_ギャップの大きい順に枠を埋める(self, gf: ModuleType) -> None:
        """候補が枠より多いとき、**|ギャップ| の大きい順**に採る。

        並び順に新しいパラメータを作らず、「乖離が大きいほど良い」という
        既存の観測（意思決定ログ56・66）に従う。

        検証は結果で行う: 候補6件・枠5件で、**最小ギャップの1件だけが
        大損する**データを与える。大きい順なら最小ギャップは溢れて
        除外されるので、損は残らない。順序が逆なら大損が入る。
        """
        pairs = (
            self._pair(gf, "WORST", DAY1, gap=0.021, move=+0.10, price=1_000.0),
            *(
                self._pair(gf, f"S{i}", DAY1, gap=gap, move=0.0, price=1_000.0)
                for i, gap in enumerate((0.03, 0.04, 0.05, 0.06, 0.07))
            ),
        )
        stats = gf.capacity_stats(pairs, 500_000, threshold=0.02)
        assert stats.mean_slots_filled == pytest.approx(5.0)
        # 残った5件は値動きゼロなので、損益は往復コスト（1,000円・呼値1円×2本=20bps）だけ。
        # 100%建玉 × 20bps × 20営業日 = -4%。WORST が入っていれば -40% 規模になる
        assert stats.monthly_return_pct == pytest.approx(-4.0, abs=0.05)

    def test_コストを引いた後の月利を返す(self, gf: ModuleType) -> None:
        """**gross ではなく net。** ここを取り違えると全部が正に見える。"""
        # フェード幅0.01%（=1bps）はコスト（1,000円で呼値1円=10bps）に負ける
        pairs = (self._pair(gf, "A", DAY1, gap=0.03, move=-0.0001),)
        stats = gf.capacity_stats(pairs, 1_000_000, threshold=0.02)
        assert stats.monthly_return_pct < 0

    def test_対象日がなければNone(self, gf: ModuleType) -> None:
        assert gf.capacity_stats((), 1_000_000) is None


class TestMarketDrift:
    """買建の優位が銘柄固有か、相場の上昇ドリフトかを切り分ける診断のテスト。

    **ここが壊れると、上昇局面を切り取っただけの見かけの優位を
    「本物の平均回帰」と誤認する。**
    """

    def _pair(
        self,
        gf: ModuleType,
        symbol: str,
        day: date,
        *,
        gap: float,
        move: float,
    ) -> Any:
        return gf.GapFadePair(
            symbol=symbol,
            day=day,
            gap_pct=gap,
            intraday_return_pct=move,
            open_price=1_000.0,
        )

    def test_平均の始値から終値をbpsで返す(self, gf: ModuleType) -> None:
        pairs = (
            self._pair(gf, "A", DAY1, gap=0.02, move=0.01),
            self._pair(gf, "B", DAY1, gap=-0.02, move=0.03),
        )
        assert gf.market_drift_bps(pairs) == pytest.approx(200.0)

    def test_ペアがなければゼロ(self, gf: ModuleType) -> None:
        assert gf.market_drift_bps(()) == 0.0

    def test_控除するとその日の平均がゼロになる(self, gf: ModuleType) -> None:
        pairs = (
            self._pair(gf, "A", DAY1, gap=0.02, move=0.01),
            self._pair(gf, "B", DAY1, gap=-0.02, move=0.03),
        )
        assert gf.market_drift_bps(gf.demean_by_day(pairs)) == pytest.approx(0.0)

    def test_控除は日ごとに行う(self, gf: ModuleType) -> None:
        """**日をまたいで平均すると、その日の市場の動きが残ってしまう。**"""
        pairs = (
            self._pair(gf, "A", DAY1, gap=0.02, move=0.10),
            self._pair(gf, "B", DAY1, gap=0.02, move=0.10),
            self._pair(gf, "A", DAY2, gap=0.02, move=-0.10),
            self._pair(gf, "B", DAY2, gap=0.02, move=-0.10),
        )
        adjusted = gf.demean_by_day(pairs)
        # 各日で全銘柄が同じ動き ＝ すべて市場要因。控除後は全件ゼロになる
        assert all(p.intraday_return_pct == pytest.approx(0.0) for p in adjusted)

    def test_控除しても銘柄固有の差は残る(self, gf: ModuleType) -> None:
        pairs = (
            self._pair(gf, "A", DAY1, gap=0.02, move=0.03),
            self._pair(gf, "B", DAY1, gap=0.02, move=0.01),
        )
        adjusted = {p.symbol: p.intraday_return_pct for p in gf.demean_by_day(pairs)}
        assert adjusted["A"] == pytest.approx(0.01)
        assert adjusted["B"] == pytest.approx(-0.01)

    def test_ギャップと始値は変えない(self, gf: ModuleType) -> None:
        """控除するのは値動きだけ。**バケット分けの基準を動かさない。**"""
        pairs = (
            self._pair(gf, "A", DAY1, gap=0.02, move=0.03),
            self._pair(gf, "B", DAY1, gap=-0.05, move=0.01),
        )
        adjusted = {p.symbol: p for p in gf.demean_by_day(pairs)}
        assert adjusted["A"].gap_pct == pytest.approx(0.02)
        assert adjusted["B"].gap_pct == pytest.approx(-0.05)
        assert adjusted["A"].open_price == pytest.approx(1_000.0)

    def test_上昇ドリフトだけなら控除で買建の優位が消える(self, gf: ModuleType) -> None:
        """**この診断が答えるべき問いそのもの。**

        全銘柄が一律に +1% 動いた日を作る。素の `fade_score` では
        ギャップダウン側（買建）が +1%、ギャップアップ側（売建）が -1% に
        なるが、これはフェードではなく相場が上がっただけ。控除後は
        両方ゼロになるのが正しい。
        """
        pairs = (
            self._pair(gf, "UP", DAY1, gap=0.03, move=0.01),
            self._pair(gf, "DOWN", DAY1, gap=-0.03, move=0.01),
        )
        raw = {p.symbol: gf.fade_score(p) for p in pairs}
        assert raw["DOWN"] == pytest.approx(0.01)
        assert raw["UP"] == pytest.approx(-0.01)

        adjusted = {p.symbol: gf.fade_score(p) for p in gf.demean_by_day(pairs)}
        assert adjusted["DOWN"] == pytest.approx(0.0)
        assert adjusted["UP"] == pytest.approx(0.0)


class TestCapacityUniverseDays:
    def test_方向を絞っても営業日の分母が縮まない(self, gf: ModuleType) -> None:
        """**ここを落とすと月利が過大に出る。**

        買建のシグナルが DAY1 にしかなくても、DAY2 は「建てなかった日」
        として分母に残さなければならない。
        """
        pairs = (
            gf.GapFadePair(
                symbol="A", day=DAY1, gap_pct=-0.03, intraday_return_pct=0.01, open_price=1_000.0
            ),
            gf.GapFadePair(
                symbol="A", day=DAY2, gap_pct=0.03, intraday_return_pct=-0.01, open_price=1_000.0
            ),
        )
        longs = tuple(p for p in pairs if p.gap_pct < 0)
        all_days = frozenset(p.day for p in pairs)

        without = gf.capacity_stats(longs, 1_000_000, threshold=0.02)
        with_denominator = gf.capacity_stats(
            longs, 1_000_000, threshold=0.02, universe_days=all_days
        )
        assert without.days == 1
        assert with_denominator.days == 2
        # 分母が2倍になれば月利は半分になる
        assert with_denominator.monthly_return_pct == pytest.approx(
            without.monthly_return_pct / 2
        )
