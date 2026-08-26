"""ドメイン型のテスト。

Phase 0 時点で実際にロジックを持つのは、建玉金額・建玉総額・スプレッド・
レバレッジ比率の計算のみ。ここを間違えるとレバレッジ判定が狂うため、
スケルトン段階でもテストしておく。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from autotrader.risk.leverage import LeverageCheck
from autotrader.types import (
    AccountState,
    MarginTradeType,
    Position,
    Quote,
    Side,
)


def _position(side: Side, qty: int, price: float) -> Position:
    return Position(
        symbol="7203",
        side=side,
        quantity=qty,
        entry_price=price,
        margin_trade_type=MarginTradeType.DAYTRADE,
        opened_at=datetime(2026, 8, 20, 9, 30),
        stop_order_id="stop-1" if side is Side.SHORT else None,
    )


class TestPositionNotional:
    def test_建玉金額は株価かける株数(self) -> None:
        pos = _position(Side.LONG, 100, 1500.0)
        assert pos.notional == Decimal("150000.0")

    def test_ショートでも建玉金額は正の値(self) -> None:
        """ショートを負値にすると、グロスの建玉総額が過小評価される。"""
        pos = _position(Side.SHORT, 100, 1500.0)
        assert pos.notional > 0


class TestGrossNotional:
    def test_建玉がなければゼロ(self) -> None:
        account = AccountState(
            cash=Decimal(500_000), positions=(), as_of=datetime(2026, 8, 20)
        )
        assert account.gross_notional == Decimal(0)

    def test_ロングとショートを相殺しない(self) -> None:
        """両建てでもリスクは合算される。ネットで見るとレバレッジ判定が甘くなる。"""
        account = AccountState(
            cash=Decimal(500_000),
            positions=(
                _position(Side.LONG, 100, 1000.0),
                _position(Side.SHORT, 100, 1000.0),
            ),
            as_of=datetime(2026, 8, 20),
        )
        # 相殺すれば 0、グロスなら 200,000
        assert account.gross_notional == Decimal("200000.0")

    def test_複数建玉を合算する(self) -> None:
        account = AccountState(
            cash=Decimal(500_000),
            positions=(
                _position(Side.LONG, 100, 1200.0),
                _position(Side.LONG, 100, 800.0),
            ),
            as_of=datetime(2026, 8, 20),
        )
        assert account.gross_notional == Decimal("200000.0")


class TestLeverageCheckRatio:
    def test_建玉総額が現金と等しければ比率は1(self) -> None:
        """レバレッジ1倍の境界。ここが 1.0 を超えてはならない。"""
        check = LeverageCheck(
            allowed=True,
            current_notional=Decimal(300_000),
            additional_notional=Decimal(200_000),
            cash=Decimal(500_000),
            reason="",
        )
        assert check.resulting_ratio == 1.0

    def test_現金を超えると比率が1を超える(self) -> None:
        check = LeverageCheck(
            allowed=False,
            current_notional=Decimal(400_000),
            additional_notional=Decimal(200_000),
            cash=Decimal(500_000),
            reason="超過",
        )
        assert check.resulting_ratio > 1.0

    def test_現金ゼロは無限大として扱う(self) -> None:
        """ゼロ除算で落とさず、明確に「許容されない」と表現する。"""
        check = LeverageCheck(
            allowed=False,
            current_notional=Decimal(0),
            additional_notional=Decimal(100_000),
            cash=Decimal(0),
            reason="現金なし",
        )
        assert check.resulting_ratio == float("inf")


class TestQuoteSpread:
    def test_スプレッドはアスク引くビッド(self) -> None:
        """手数料0でもスプレッドは実質コスト。0 として扱ってはならない。"""
        quote = Quote(
            symbol="7203",
            timestamp=datetime(2026, 8, 20, 9, 30),
            bid=1499.0,
            ask=1501.0,
            last=1500.0,
            bid_size=100,
            ask_size=100,
        )
        assert quote.spread == 2.0


class TestUniverseEntryStage:
    def test_StageAでは寄り前気配がNoneでよい(self) -> None:
        """口座なしの Stage A では寄り前気配が取れない。

        必須にすると Stage A で UniverseEntry を作れなくなる。
        """
        from datetime import date

        from autotrader.types import PriceTier, Symbol, UniverseEntry

        entry = UniverseEntry(
            symbol=Symbol(code="7203", name="トヨタ"),
            trade_date=date(2026, 8, 20),
            price_tier=PriceTier.NORMAL,
            score=0.8,
            atr_pct=0.025,
            prev_volume_ratio=1.4,
            prev_range_pct=0.03,
            prev_close_position=0.7,
        )
        assert entry.gap_pct is None
        assert entry.premarket_volume_ratio is None

    def test_StageBでは寄り前気配を持てる(self) -> None:
        from datetime import date

        from autotrader.types import PriceTier, Symbol, UniverseEntry

        entry = UniverseEntry(
            symbol=Symbol(code="7203", name="トヨタ"),
            trade_date=date(2026, 8, 20),
            price_tier=PriceTier.NORMAL,
            score=0.8,
            atr_pct=0.025,
            prev_volume_ratio=1.4,
            prev_range_pct=0.03,
            prev_close_position=0.7,
            gap_pct=0.012,
            premarket_volume_ratio=2.1,
        )
        assert entry.gap_pct == 0.012


class TestSymbolIsTopix100:
    """**呼値がTOPIX100かどうかで変わる**ので、判定はコストに直結する。

    900円の銘柄なら通常は呼値1円で往復22bps、TOPIX100 は 0.1円で往復2.2bps
    （`docs/00-overview.md` 意思決定ログ61）。
    """

    def test_Core30とLarge70をTOPIX100とみなす(self) -> None:
        from autotrader.types import Symbol

        for category in ("TOPIX Core30", "TOPIX Large70"):
            assert Symbol(code="7203", name="x", scale_category=category).is_topix100

    def test_Mid400やSmallは含まない(self) -> None:
        from autotrader.types import Symbol

        for category in ("TOPIX Mid400", "TOPIX Small 1", "TOPIX Small 2", "-"):
            assert not Symbol(code="7203", name="x", scale_category=category).is_topix100

    def test_規模区分が不明ならFalse(self) -> None:
        """**取れていないものを「細かい呼値」と誤認しない。**

        判定を誤ると、コストを実際より小さく見積もる側に倒れる。
        """
        from autotrader.types import Symbol

        assert not Symbol(code="7203", name="x").is_topix100
        assert not Symbol(code="7203", name="x", scale_category="").is_topix100

    def test_表記揺れに耐える(self) -> None:
        """空白の有無・大文字小文字で判定が変わらないこと。"""
        from autotrader.types import Symbol

        for category in ("TOPIXCore30", "topix core30", "TOPIX　Large70"):
            assert Symbol(code="7203", name="x", scale_category=category).is_topix100
