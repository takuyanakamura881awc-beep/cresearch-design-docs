"""autotrader.data.external のテスト。

**重点は `align_prior_session`。** ここが緩むと、まだ出ていない米国終値で
東京の寄り付きを予測することになる——5分足のラベル解釈で踏んだのと
同じ形の先読み（意思決定ログ81）。

残り2つ:

- **出来高が無い系列を捨てないこと。** 指数・為替は出来高を持たないので、
  OHLCV 全部に `pd.notna()` を要求すると**エラーなく空になる**
- **価格の欠損は捨てること。** 出来高は緩めるが、4本値は緩めない
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from autotrader.data.external import (
    DEFAULT_SERIES,
    ExternalSeries,
    align_prior_session,
    log_returns,
    trailing_rank,
)
from autotrader.types import Bar


def _bar(day: date, close: float, *, volume: int = 0) -> Bar:
    return Bar(
        symbol="VIX",
        timestamp=datetime(day.year, day.month, day.day),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
    )


class TestAlignPriorSession:
    """**外部日付 < 日本の営業日**（厳密に小さい）。等号は先読み。"""

    def test_前営業日の終値を使う(self) -> None:
        us = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]
        jp = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
        assert align_prior_session(us, jp) == {
            date(2026, 9, 22): date(2026, 9, 21),
            date(2026, 9, 23): date(2026, 9, 22),
            date(2026, 9, 24): date(2026, 9, 23),
        }

    def test_同じ日付の外部終値は使わない(self) -> None:
        """**ここが核心。** 米国 date T の終値は東京 date T の大引けより後に出る。"""
        us = [date(2026, 9, 24)]
        jp = [date(2026, 9, 24)]
        assert align_prior_session(us, jp) == {}

    def test_日本が休みで海外が動いた日は直近まで遡る(self) -> None:
        """日本の連休中に海外が3日動いたら、**その最後の終値**を使う。"""
        us = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]
        jp = [date(2026, 9, 24)]
        assert align_prior_session(us, jp) == {date(2026, 9, 24): date(2026, 9, 23)}

    def test_海外が休みなら前の営業日の終値を使い回す(self) -> None:
        """海外の祝日。**その日を飛ばすのではなく、直近の確定値を使う。**"""
        us = [date(2026, 9, 21)]
        jp = [date(2026, 9, 22), date(2026, 9, 23)]
        assert align_prior_session(us, jp) == {
            date(2026, 9, 22): date(2026, 9, 21),
            date(2026, 9, 23): date(2026, 9, 21),
        }

    def test_系列の先頭より前の日は含めない(self) -> None:
        """**無い日を黙って埋めない。** 対応が無ければキーごと落とす。"""
        us = [date(2026, 9, 23)]
        jp = [date(2026, 9, 22), date(2026, 9, 24)]
        assert align_prior_session(us, jp) == {date(2026, 9, 24): date(2026, 9, 23)}

    def test_順不同で渡しても結果が変わらない(self) -> None:
        us = [date(2026, 9, 23), date(2026, 9, 21), date(2026, 9, 22)]
        jp = [date(2026, 9, 24), date(2026, 9, 22)]
        assert align_prior_session(us, jp) == {
            date(2026, 9, 22): date(2026, 9, 21),
            date(2026, 9, 24): date(2026, 9, 23),
        }

    def test_空でも落ちない(self) -> None:
        assert align_prior_session([], [date(2026, 9, 24)]) == {}
        assert align_prior_session([date(2026, 9, 24)], []) == {}


class TestLogReturns:
    def test_前営業日からの対数リターン(self) -> None:
        import math

        bars = [
            _bar(date(2026, 9, 21), 100.0),
            _bar(date(2026, 9, 22), 110.0),
        ]
        got = log_returns(bars)
        assert got == {date(2026, 9, 22): pytest.approx(math.log(1.1))}

    def test_初日は含めない(self) -> None:
        bars = [_bar(date(2026, 9, 21), 100.0)]
        assert log_returns(bars) == {}

    def test_終値が0以下の日は飛ばす(self) -> None:
        """0除算と対数の定義域。**握り潰さず、その日だけ落とす。**"""
        bars = [
            _bar(date(2026, 9, 21), 100.0),
            _bar(date(2026, 9, 22), 0.0),
            _bar(date(2026, 9, 23), 110.0),
        ]
        assert log_returns(bars) == {}

    def test_並び順に依存しない(self) -> None:
        import math

        bars = [
            _bar(date(2026, 9, 22), 110.0),
            _bar(date(2026, 9, 21), 100.0),
        ]
        assert log_returns(bars)[date(2026, 9, 22)] == pytest.approx(math.log(1.1))


class TestDefaultSeries:
    def test_キーはファイル名に使える文字だけ(self) -> None:
        """``^`` や ``=`` を保存キーに混ぜない（Windows でも安全に）。"""
        for spec in DEFAULT_SERIES:
            assert spec.key.isalnum(), spec.key

    def test_ティッカーは日本株の変換を通さない(self) -> None:
        """``.T`` を付けてしまうと別物を取りに行く。"""
        for spec in DEFAULT_SERIES:
            assert not spec.ticker.endswith(".T"), spec.ticker

    def test_キーが重複しない(self) -> None:
        keys = [s.key for s in DEFAULT_SERIES]
        assert len(keys) == len(set(keys))

    def test_本数を絞ってある(self) -> None:
        """**増やすと多重比較の分母が増える。** 足すときは事前登録する。"""
        assert len(DEFAULT_SERIES) == 5

    def test_VIXが含まれる(self) -> None:
        """本命の仮説（Nagel 2012）が VIX に乗っている。"""
        assert any(s.key == "VIX" for s in DEFAULT_SERIES)

    def test_データクラスは不変(self) -> None:
        spec = ExternalSeries("X", "^X", "test")
        with pytest.raises(AttributeError):
            spec.key = "Y"  # type: ignore[misc]


class TestTrailingRank:
    """**比較対象はその日より前の観測だけ。** 全期間の分位点は事後診断になる。

    レジーム診断で「事後診断 → 先読み版フィルタ」の二段構えに入り、
    持続性が測れず打ち止めになった（意思決定ログ47〜50）。同じ回り道をしない。
    """

    def _series(self, values: list[float]) -> dict[date, float]:
        base = date(2026, 1, 1)
        return {base + timedelta(days=i): v for i, v in enumerate(values)}

    def test_過去が足りない日は含めない(self) -> None:
        got = trailing_rank(self._series([1.0, 2.0, 3.0]), window=3)
        assert got == {}

    def test_過去より大きければ上位(self) -> None:
        got = trailing_rank(self._series([1.0, 2.0, 3.0, 99.0]), window=3)
        assert got[date(2026, 1, 4)] == pytest.approx(1.0)

    def test_過去より小さければ下位(self) -> None:
        got = trailing_rank(self._series([10.0, 20.0, 30.0, 1.0]), window=3)
        assert got[date(2026, 1, 4)] == pytest.approx(0.0)

    def test_中間なら中間の順位(self) -> None:
        got = trailing_rank(self._series([10.0, 20.0, 30.0, 25.0]), window=3)
        assert got[date(2026, 1, 4)] == pytest.approx(2 / 3)

    def test_未来の観測を使わない(self) -> None:
        """**核心。** 後から巨大な値を足しても、それ以前の日の順位は動かない。"""
        short = trailing_rank(self._series([1.0, 2.0, 3.0, 4.0]), window=3)
        long = trailing_rank(self._series([1.0, 2.0, 3.0, 4.0, 999.0]), window=3)
        assert long[date(2026, 1, 4)] == pytest.approx(short[date(2026, 1, 4)])

    def test_窓は直近だけを見る(self) -> None:
        """窓の外に出た古い観測は比較対象から外れる。"""
        got = trailing_rank(self._series([99.0, 1.0, 2.0, 3.0]), window=2)
        # 4日目の比較対象は [1.0, 2.0]（99.0 は窓の外）なので最上位
        assert got[date(2026, 1, 4)] == pytest.approx(1.0)
