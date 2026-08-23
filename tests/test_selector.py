"""Layer 2（日次銘柄選定）のテスト。

重点は3つ。

1. **ルックアヘッドを構造的に防げているか** — 当日のバーを混ぜても結果が変わらないこと
2. **順位変換がスケール差を吸収しているか** — 生値の加重合計に退化していないこと
3. **プレミアム枠のハードルが効いているか** — 1単元が資金の60%を占める枠を安易に採らないこと
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from autotrader.risk.limits import max_atr_pct
from autotrader.types import Bar, PriceTier, Symbol
from autotrader.universe.selector import (
    DEFAULT_STAGE_A_WEIGHTS,
    Candidate,
    SelectorConfig,
    StageAFeatures,
    StageBFeatures,
    build_candidates,
    compute_atr,
    compute_features,
    rank_normalize,
    score_all,
    select,
    true_range,
    validate_weights,
)

START = date(2026, 5, 1)


def _bar(
    code: str,
    day: int,
    close: float = 1000.0,
    high: float | None = None,
    low: float | None = None,
    volume: int = 100_000,
) -> Bar:
    return Bar(
        symbol=code,
        timestamp=datetime(2026, 5, 1) + timedelta(days=day),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
    )


def _bars(
    code: str = "7203",
    n: int = 25,
    close: float = 1000.0,
    range_pct: float = 0.03,
    volume: int = 100_000,
) -> tuple[Bar, ...]:
    """日々 ``range_pct`` の値幅を持つ、終値一定のバー列。

    終値を動かさないことで ATR が値幅だけで決まり、期待値を手で計算できる。
    """
    half = close * range_pct / 2
    return tuple(
        _bar(code, i, close=close, high=close + half, low=close - half, volume=volume)
        for i in range(n)
    )


def _features(
    atr_pct: float = 0.03,
    prev_volume_ratio: float = 1.0,
    prev_range_pct: float = 0.03,
    prev_close_position: float = 0.5,
    price: float = 1000.0,
) -> StageAFeatures:
    # ATR円は ATR% と株価から決まる。コスト下限はこちらで判定される
    return StageAFeatures(
        atr_pct=atr_pct,
        price=price,
        atr_yen=atr_pct * price,
        prev_volume_ratio=prev_volume_ratio,
        prev_range_pct=prev_range_pct,
        prev_close_position=prev_close_position,
    )


def _candidate(
    code: str,
    tier: PriceTier = PriceTier.NORMAL,
    stage_b: StageBFeatures | None = None,
    **kwargs: float,
) -> Candidate:
    return Candidate(
        symbol=Symbol(code=code, name=code, market="プライム", margin_type="貸借"),
        tier=tier,
        features=_features(**kwargs),
        stage_b=stage_b,
    )


class TestTrueRange:
    def test_ギャップを含めて測る(self) -> None:
        """単純な高値−安値ではギャップアップした日の値幅を取りこぼす。"""
        bar = _bar("7203", 1, close=1100.0, high=1110.0, low=1090.0)
        assert true_range(bar, prev_close=1090.0) == pytest.approx(20.0)
        # 前日終値 1000 から窓を開けて始まった → 実際の値幅は 110
        assert true_range(bar, prev_close=1000.0) == pytest.approx(110.0)


class TestAtr:
    def test_値幅一定ならATRはその値幅になる(self) -> None:
        bars = _bars(n=25, close=1000.0, range_pct=0.03)
        assert compute_atr(bars, period=14) == pytest.approx(30.0)

    def test_本数が足りなければNoneを返す(self) -> None:
        """**足りないのに計算して返さない。**

        短い期間の平均は値幅の推定として信用できず、
        上場直後の銘柄が誤って上位に来る。
        """
        assert compute_atr(_bars(n=14), period=14) is None
        assert compute_atr(_bars(n=15), period=14) is not None

    def test_直近期間だけを使う(self) -> None:
        """古い大きな値幅を引きずらない。"""
        old = list(_bars(n=10, range_pct=0.20))
        recent = [
            _bar("7203", 10 + i, close=1000.0, high=1005.0, low=995.0) for i in range(15)
        ]
        assert compute_atr(tuple(old + recent), period=14) == pytest.approx(10.0)


class TestComputeFeatures:
    def test_4指標を計算する(self) -> None:
        bars = list(_bars(n=24, close=1000.0, range_pct=0.03, volume=100_000))
        # 前日だけ出来高2倍・終値は高値引け
        bars.append(_bar("7203", 24, close=1015.0, high=1015.0, low=985.0, volume=300_000))
        f = compute_features(tuple(bars), atr_period=14, volume_lookback_days=20)

        assert f is not None
        assert f.prev_range_pct == pytest.approx(30.0 / 1015.0)
        assert f.prev_close_position == pytest.approx(1.0)  # 高値引け
        # 20日平均出来高 = (19本 × 10万 + 30万) / 20 = 11万
        assert f.prev_volume_ratio == pytest.approx(300_000 / 110_000)
        assert f.atr_pct > 0

    def test_本数が足りなければNoneを返す(self) -> None:
        assert compute_features(_bars(n=19), volume_lookback_days=20) is None
        assert compute_features(()) is None

    def test_値幅ゼロの終値位置は05にする(self) -> None:
        """ストップ張り付きで高値=安値のとき、0 や 1 に倒すと方向を捏造する。"""
        bars = tuple(_bar("7203", i, close=1000.0) for i in range(25))
        f = compute_features(bars)
        assert f is not None
        assert f.prev_close_position == 0.5
        assert f.prev_range_pct == 0.0

    def test_出来高ゼロでもゼロ除算しない(self) -> None:
        bars = tuple(
            _bar("7203", i, close=1000.0, high=1010.0, low=990.0, volume=0)
            for i in range(25)
        )
        f = compute_features(bars)
        assert f is not None
        assert f.prev_volume_ratio == 0.0


class TestRankNormalize:
    def test_ゼロから1に収まる(self) -> None:
        result = rank_normalize({"a": 5.0, "b": 1.0, "c": 3.0})
        assert result == {"b": 0.0, "c": 0.5, "a": 1.0}

    def test_同値には平均順位を与える(self) -> None:
        """並び順という恣意的な要素で差がついてはならない。"""
        result = rank_normalize({"a": 1.0, "b": 1.0, "c": 2.0, "d": 3.0})
        assert result["a"] == result["b"] == pytest.approx(0.5 / 3)
        assert result["d"] == 1.0

    def test_全部同値なら全員同じ値になる(self) -> None:
        """全員が平均順位 = 0.5 になる。

        1.0（全員が最上位）にすると、その指標が「全員に満点」を配ることになり、
        重みぶんスコアが底上げされてプレミアム枠のハードルが甘くなる。
        """
        result = rank_normalize({"a": 1.0, "b": 1.0, "c": 1.0})
        assert set(result.values()) == {0.5}

    def test_1銘柄のときは05にする(self) -> None:
        """順位差が定義できない。"""
        assert rank_normalize({"a": 7.0}) == {"a": 0.5}

    def test_空なら空(self) -> None:
        assert rank_normalize({}) == {}


class TestValidateWeights:
    def test_未知の指標名を弾く(self) -> None:
        """**綴り違いを黙って通さない。**

        通すとその指標の寄与が静かに 0 になり、
        スコアが変わったことに誰も気づけない。
        """
        with pytest.raises(ValueError, match="未知の指標名"):
            validate_weights({"atr_percent": 1.0})

    def test_合計が1でなければ弾く(self) -> None:
        with pytest.raises(ValueError, match="合計"):
            validate_weights({"atr_pct": 0.5, "prev_range_pct": 0.2})

    def test_負の重みを弾く(self) -> None:
        with pytest.raises(ValueError, match="負"):
            validate_weights({"atr_pct": 1.5, "prev_range_pct": -0.5})

    def test_既定の重みは妥当(self) -> None:
        validate_weights(DEFAULT_STAGE_A_WEIGHTS)


class TestScoreAll:
    def test_スケール差が支配しない(self) -> None:
        """**生値の加重合計に退化していないことの確認。**

        ATR%(0.02前後) と出来高比(1.0前後) はスケールが2桁違う。
        生値を足すと、重みに関係なく出来高比が支配する。
        ここでは ATR% だけが優れた銘柄が、重み0.4ぶん確実に上に来ることを見る。
        """
        features = {
            "A": _features(atr_pct=0.10, prev_volume_ratio=1.0),
            "B": _features(atr_pct=0.02, prev_volume_ratio=5.0),
        }
        scores = score_all(features, {"atr_pct": 0.7, "prev_volume_ratio": 0.3})
        assert scores["A"] == pytest.approx(0.7)
        assert scores["B"] == pytest.approx(0.3)

    def test_全指標が最上位なら1になる(self) -> None:
        features = {
            "A": _features(0.10, 5.0, 0.10, 1.0),
            "B": _features(0.01, 0.5, 0.01, 0.0),
        }
        scores = score_all(features)
        assert scores["A"] == pytest.approx(1.0)
        assert scores["B"] == pytest.approx(0.0)

    def test_StageBの重みなのに気配がなければ弾く(self) -> None:
        with pytest.raises(ValueError, match="寄り前気配が渡されていない"):
            score_all({"A": _features()}, {"atr_pct": 0.5, "gap_pct": 0.5})

    def test_気配が一部だけなら弾く(self) -> None:
        """一部だけ気配があると、その銘柄が構造的に有利になる。"""
        with pytest.raises(ValueError, match="欠けている"):
            score_all(
                {"A": _features(), "B": _features()},
                stage_b_by_symbol={"A": StageBFeatures(0.01, 1.0)},
            )

    def test_空なら空(self) -> None:
        assert score_all({}) == {}


class TestLookahead:
    """**ルックアヘッドは構造で防ぐ（docs/03-universe.md §4.3）。**

    「気をつける」ではなく「起こせない」ことを確認する。
    """

    def test_当日のバーを混ぜても結果が変わらない(self) -> None:
        code = "7203"
        symbols = [Symbol(code=code, name="テスト", market="プライム", margin_type="貸借")]
        tiers = {code: PriceTier.NORMAL}
        past = _bars(code, n=25, close=1000.0, range_pct=0.03)
        trade_date = past[-1].timestamp.date() + timedelta(days=1)

        # 当日に大きく動いたバー。これを見られるなら指標が変わるはず
        today = Bar(
            symbol=code,
            timestamp=datetime.combine(trade_date, datetime.min.time()),
            open=1000.0,
            high=2000.0,
            low=500.0,
            close=1900.0,
            volume=99_000_000,
        )

        clean = build_candidates(trade_date, symbols, {code: past}, tiers)
        polluted = build_candidates(trade_date, symbols, {code: (*past, today)}, tiers)

        assert clean[0].features == polluted[0].features

    def test_過去のバーだけでも足りなければ落とす(self) -> None:
        """当日を除いた結果、本数が足りなくなる銘柄は採らない。"""
        code = "7203"
        symbols = [Symbol(code=code, name="テスト", market="プライム", margin_type="貸借")]
        past = _bars(code, n=19)
        trade_date = past[-1].timestamp.date() + timedelta(days=1)
        assert build_candidates(trade_date, symbols, {code: past}, {code: PriceTier.NORMAL}) == ()

    def test_枠が不明な銘柄は落とす(self) -> None:
        code = "7203"
        symbols = [Symbol(code=code, name="テスト", market="プライム", margin_type="貸借")]
        past = _bars(code, n=25)
        trade_date = past[-1].timestamp.date() + timedelta(days=1)
        assert build_candidates(trade_date, symbols, {code: past}, {}) == ()


class TestSelect:
    TRADE_DATE = date(2026, 6, 1)

    def test_スコア降順で返す(self) -> None:
        candidates = [
            _candidate("A", atr_pct=0.03),
            _candidate("B", atr_pct=0.05),
            _candidate("C", atr_pct=0.04),
        ]
        picked = select(candidates, self.TRADE_DATE)
        assert [e.symbol.code for e in picked] == ["B", "C", "A"]

    def test_ATRが足りない銘柄を落とす(self) -> None:
        """日中値幅がスプレッドの5倍ないとコスト負けする。

        1,000円なら呼値1円 × 2本 = スプレッド2円。その5倍の10円が下限で、
        ATR% にすると 1.0%。
        """
        candidates = [_candidate("A", atr_pct=0.009), _candidate("B", atr_pct=0.011)]
        picked = select(candidates, self.TRADE_DATE)
        assert [e.symbol.code for e in picked] == ["B"]

    def test_同じATRパーセントでもATR円が大きい銘柄が上位(self) -> None:
        """**スコアを ATR% から ATR円 に差し替えた理由。**

        往復コストは ``スプレッド円 ÷ ATR円``。ATR% が同じでも株価が
        違えばコストは違う。実測では監視50銘柄の中で2.3倍ばらついていた。
        """
        rich = _candidate("RICH", atr_pct=0.033, price=1200.0)   # ATR 39.6円
        poor = _candidate("POOR", atr_pct=0.033, price=400.0)    # ATR 13.2円
        picked = select([rich, poor], self.TRADE_DATE)
        assert [e.symbol.code for e in picked] == ["RICH", "POOR"]

    def test_旧スコアなら株価を見ない(self) -> None:
        """`--legacy-score` 相当。ATR% が同点なら他の指標で決まる。

        差し替えの A/B が成立する条件なので固定しておく。
        """
        legacy = SelectorConfig(
            weights={
                "atr_pct": 0.40,
                "prev_volume_ratio": 0.25,
                "prev_range_pct": 0.20,
                "prev_close_position": 0.15,
            }
        )
        rich = _candidate("RICH", atr_pct=0.033, price=1200.0)
        poor = _candidate("POOR", atr_pct=0.033, price=400.0)
        scores = {
            e.symbol.code: e.score for e in select([rich, poor], self.TRADE_DATE, legacy)
        }
        assert scores["RICH"] == pytest.approx(scores["POOR"])

    def test_下限は株価で動く(self) -> None:
        """**これが ATR% を捨てて円建てにした理由。**

        同じ ATR 0.9% でも、2,200円なら 19.8円で下限10円を超えるが、
        600円なら 5.4円で足りない。固定の 2% では両方を落としてしまう。
        """
        rich = _candidate("RICH", atr_pct=0.009, price=2200.0)
        poor = _candidate("POOR", atr_pct=0.009, price=600.0)
        picked = select([rich, poor], self.TRADE_DATE)
        assert [e.symbol.code for e in picked] == ["RICH"]

    def test_ATRの足切りはスコア計算の後に行う(self) -> None:
        """**順位変換の母集団を先に削ってはならない。**

        先に削ると、残った銘柄の順位が「何を削ったか」で変わる。
        ここでは足切り対象を増やしても、残る2銘柄の相対スコアが不変であることを見る。
        """
        keep = [_candidate("A", atr_pct=0.05), _candidate("B", atr_pct=0.03)]
        with_junk = [*keep, _candidate("Z", atr_pct=0.001), _candidate("Y", atr_pct=0.002)]

        a = {e.symbol.code: e.score for e in select(keep, self.TRADE_DATE)}
        b = {e.symbol.code: e.score for e in select(with_junk, self.TRADE_DATE)}
        assert a.keys() == b.keys() == {"A", "B"}
        assert a["A"] > a["B"] and b["A"] > b["B"]

    def test_全滅したら空を返す(self) -> None:
        assert select([_candidate("A", atr_pct=0.001)], self.TRADE_DATE) == ()

    def test_監視枠の上限で切る(self) -> None:
        """上限50は Stage B の WebSocket 制限。Stage A も同じ数に揃える。"""
        candidates = [
            _candidate(f"{1000 + i}", atr_pct=0.021 + i * 0.0005) for i in range(60)
        ]
        picked = select(candidates, self.TRADE_DATE)
        assert len(picked) == 50

    def test_同点は銘柄コード順で決める(self) -> None:
        """実行のたびに並びが変わると検証結果が再現しない。"""
        candidates = [_candidate("9999"), _candidate("1111"), _candidate("5555")]
        picked = select(candidates, self.TRADE_DATE)
        assert [e.symbol.code for e in picked] == ["1111", "5555", "9999"]

    def test_銘柄コードの重複を拒否する(self) -> None:
        with pytest.raises(ValueError, match="重複"):
            select([_candidate("A"), _candidate("A")], self.TRADE_DATE)

    def test_空なら空(self) -> None:
        assert select([], self.TRADE_DATE) == ()

    def test_指標が結果に載る(self) -> None:
        picked = select([_candidate("A", prev_volume_ratio=2.5)], self.TRADE_DATE)
        assert picked[0].prev_volume_ratio == 2.5
        assert picked[0].trade_date == self.TRADE_DATE
        assert picked[0].price_tier is PriceTier.NORMAL
        assert picked[0].gap_pct is None  # Stage A


class TestAtrCeiling:
    """**ATR% の上限。下限とは根拠がまったく別。**

    下限（2%）はコスト負けの回避。
    上限（5.33%）は「1敗で日次ブレーカー（-2%）に達する銘柄を採らない」ため。

    スコアは ATR% に最大の重み（0.40）を置いているので、**上限がないと
    選定自体が最も危険な銘柄を上位に押し上げる**。実測（2026-05-29）では
    ATR% の最大が17.12%で、上位10銘柄のうち2つが1敗 -2% を超えていた。
    """

    TRADE_DATE = date(2026, 6, 1)

    def test_既定値は日次ブレーカーからの導出値(self) -> None:
        """定数を直書きせず導出する。ブレーカーを動かせば自動で追随する。"""
        assert SelectorConfig().max_atr_pct == max_atr_pct()
        assert max_atr_pct() == pytest.approx(0.02 / (0.25 * 1.5))

    def test_ボラが高すぎる銘柄を落とす(self) -> None:
        candidates = [_candidate("A", atr_pct=0.05), _candidate("B", atr_pct=0.06)]
        picked = select(candidates, self.TRADE_DATE)
        assert [e.symbol.code for e in picked] == ["A"]

    def test_境界値(self) -> None:
        ceiling = max_atr_pct()
        assert select([_candidate("A", atr_pct=ceiling)], self.TRADE_DATE)
        assert select([_candidate("A", atr_pct=ceiling * 1.001)], self.TRADE_DATE) == ()

    def test_上限で全滅したら空を返す(self) -> None:
        assert select([_candidate("A", atr_pct=0.20)], self.TRADE_DATE) == ()

    def test_不正な下限を拒否する(self) -> None:
        with pytest.raises(ValueError, match="min_atr_cost_multiple"):
            SelectorConfig(min_atr_cost_multiple=0.0)
        with pytest.raises(ValueError, match="spread_ticks"):
            SelectorConfig(spread_ticks=0.0)
        with pytest.raises(ValueError, match="max_atr_pct"):
            SelectorConfig(max_atr_pct=0.0)

    def test_ブレーカーを緩めれば上限も上がる(self) -> None:
        """安全装置を動かしたときに、導出値が置き去りにならないことの確認。"""
        assert max_atr_pct(daily_breaker_pct=0.03) == pytest.approx(0.08)
        assert max_atr_pct(max_weight_per_symbol=0.20) == pytest.approx(0.0666, abs=1e-4)

    def test_不正な引数を拒否する(self) -> None:
        with pytest.raises(ValueError):
            max_atr_pct(max_weight_per_symbol=0)
        with pytest.raises(ValueError):
            max_atr_pct(stop_atr_mult=0)


class TestPremiumTier:
    """1単元が資金の40〜60%を占める枠。**安易に採らない。**"""

    TRADE_DATE = date(2026, 6, 1)

    def _mixed(self, premium_atr: float) -> list[Candidate]:
        """ATR% は上限（5.33%）の内側に収める。超えると枠の判定より先に落ちる。"""
        normal = [_candidate(f"N{i}", atr_pct=0.03 + i * 0.001) for i in range(5)]
        premium = _candidate("P1", tier=PriceTier.PREMIUM, atr_pct=premium_atr)
        return [*normal, premium]

    def test_ハードルを超えれば採用する(self) -> None:
        picked = select(self._mixed(premium_atr=0.05), self.TRADE_DATE)
        assert "P1" in [e.symbol.code for e in picked]

    def test_ハードルに届かなければ採用しない(self) -> None:
        """通常枠の中央値 × 1.3 に届かない高株価銘柄は集中リスクに見合わない。"""
        picked = select(self._mixed(premium_atr=0.0305), self.TRADE_DATE)
        assert "P1" not in [e.symbol.code for e in picked]

    def test_同時採用数の上限を守る(self) -> None:
        normal = [_candidate(f"N{i}", atr_pct=0.03 + i * 0.0001) for i in range(5)]
        premium = [
            _candidate(f"P{i}", tier=PriceTier.PREMIUM, atr_pct=0.045 + i * 0.003)
            for i in range(3)
        ]
        picked = select([*normal, *premium], self.TRADE_DATE)
        n_premium = sum(1 for e in picked if e.price_tier is PriceTier.PREMIUM)
        assert n_premium == 1

    def test_通常枠が空ならプレミアム枠も採らない(self) -> None:
        """ハードルの基準（通常枠の中央値）が定義できない。

        基準がない状態で例外枠を採るのは保守的でない。
        """
        picked = select(
            [_candidate("P1", tier=PriceTier.PREMIUM, atr_pct=0.05)], self.TRADE_DATE
        )
        assert picked == ()

    def test_無効にできる(self) -> None:
        """Phase 3 で寄与しないと分かったら枠ごと落とす。"""
        config = SelectorConfig(premium_enabled=False)
        picked = select(self._mixed(premium_atr=0.05), self.TRADE_DATE, config)
        assert "P1" not in [e.symbol.code for e in picked]

    def test_プレミアムを入れても枠の総数は超えない(self) -> None:
        config = SelectorConfig(max_watchlist=5)
        picked = select(self._mixed(premium_atr=0.05), self.TRADE_DATE, config)
        assert len(picked) == 5
        assert "P1" in [e.symbol.code for e in picked]


class TestSelectorConfig:
    def test_不正な重みを拒否する(self) -> None:
        with pytest.raises(ValueError):
            SelectorConfig(weights={"atr_pct": 0.5})

    def test_枠の上限はゼロを許さない(self) -> None:
        with pytest.raises(ValueError):
            SelectorConfig(max_watchlist=0)

    def test_既定値はconfigのyamlと一致する(self) -> None:
        """設定ファイルとコードの既定値がずれると、
        どちらが効いているのか分からなくなる。
        """
        cfg = SelectorConfig()
        assert cfg.max_watchlist == 50
        assert cfg.min_atr_cost_multiple == 5.0
        assert cfg.spread_ticks == 2.0
        assert cfg.atr_period == 14
        assert cfg.premium_score_multiplier == 1.3
        assert cfg.premium_max_concurrent == 1
        assert cfg.weights == DEFAULT_STAGE_A_WEIGHTS

    def test_指標に必要な日足の本数(self) -> None:
        assert SelectorConfig(atr_period=14, volume_lookback_days=20).min_bars == 20
        assert SelectorConfig(atr_period=30, volume_lookback_days=20).min_bars == 31


class TestStageB:
    """寄り前気配を使う経路。Stage B で予測力を比較検証してから有効にする。"""

    TRADE_DATE = date(2026, 6, 1)

    def test_気配が揃っていれば結果に載る(self) -> None:
        weights = {
            "atr_pct": 0.5,
            "prev_volume_ratio": 0.2,
            "gap_pct": 0.2,
            "premarket_volume_ratio": 0.1,
        }
        candidates = [
            _candidate("A", stage_b=StageBFeatures(0.03, 2.0), atr_pct=0.05),
            _candidate("B", stage_b=StageBFeatures(-0.01, 0.5), atr_pct=0.03),
        ]
        picked = select(candidates, self.TRADE_DATE, SelectorConfig(weights=weights))
        assert [e.symbol.code for e in picked] == ["A", "B"]
        assert picked[0].gap_pct == 0.03
        assert picked[0].premarket_volume_ratio == 2.0
