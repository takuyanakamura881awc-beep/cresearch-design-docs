"""レバレッジ1倍の強制（安全装置 #1）。

本システムの安全性の土台。docs/02-margin-rules.md の不変条件をコードで守る。

    建玉総額（買建 + 売建の合計） ≤ 現金残高

信用取引を使いながら使用金額を現物と同水準に抑えることで、
リスクを現物並みに保ったまま、空売り・当日複数回転・手数料0のメリットだけを取り込む。

【重要】全発注は必ず ``check`` を通ること。バイパス経路を作ってはならない。
リスク監査（.claude/agents/risk-auditor.md）はこの点を最優先で検証する。

**バックテストも例外ではない。** ここを通さないバックテストは、
実運用では発注できない建玉で成績を出すことになり、結果が再現しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from autotrader.types import AccountState


class LeverageViolationError(Exception):
    """レバレッジ1倍の不変条件に違反する発注が試みられた。

    握り潰してはならない。この例外が出たら発注を中止する。
    """


@dataclass(frozen=True)
class LeverageCheck:
    """レバレッジ判定の結果。監査ログに残せるよう内訳を保持する。"""

    allowed: bool
    current_notional: Decimal
    """現在の建玉総額"""
    additional_notional: Decimal
    """これから発注しようとしている金額"""
    cash: Decimal
    reason: str

    @property
    def resulting_ratio(self) -> float:
        """発注後のレバレッジ比率。1.0 を超えてはならない。"""
        if self.cash == 0:
            return float("inf")
        return float((self.current_notional + self.additional_notional) / self.cash)


def check(
    account: AccountState,
    additional_notional: Decimal,
    max_ratio: float = 1.0,
) -> LeverageCheck:
    """新規発注がレバレッジ上限を超えないか判定する。

    ロングとショートを相殺しない（グロスで見る）点に注意。
    両建てでもリスクは合算されるため。

    Args:
        account: 現在の口座状態。
        additional_notional: 新規発注の金額（円）。
        max_ratio: レバレッジ上限。既定は 1.0（変更しないこと）。

    Returns:
        判定結果。``allowed`` が False なら発注してはならない。

    Note:
        **現金残高が 0 以下なら、金額に関わらず必ず拒否する。**
        比率を計算するとゼロ除算になる領域を、判定の入口で塞いでおく。
    """
    current = account.gross_notional
    resulting = current + additional_notional

    if additional_notional < 0:
        return LeverageCheck(
            allowed=False,
            current_notional=current,
            additional_notional=additional_notional,
            cash=account.cash,
            reason="発注金額が負。返済は建玉の減少として扱い、ここには渡さない",
        )

    if account.cash <= 0:
        return LeverageCheck(
            allowed=False,
            current_notional=current,
            additional_notional=additional_notional,
            cash=account.cash,
            reason=f"現金残高が {account.cash} 円。新規建玉を作れない",
        )

    limit = account.cash * Decimal(str(max_ratio))
    allowed = resulting <= limit
    if allowed:
        reason = (
            f"建玉 {current:,.0f} + 新規 {additional_notional:,.0f} "
            f"= {resulting:,.0f} ≤ 上限 {limit:,.0f}"
        )
    else:
        reason = (
            f"レバレッジ上限違反: 建玉 {current:,.0f} + 新規 {additional_notional:,.0f} "
            f"= {resulting:,.0f} > 上限 {limit:,.0f}（現金 {account.cash:,.0f} × {max_ratio}）"
        )
    return LeverageCheck(
        allowed=allowed,
        current_notional=current,
        additional_notional=additional_notional,
        cash=account.cash,
        reason=reason,
    )


def enforce(
    account: AccountState,
    additional_notional: Decimal,
    max_ratio: float = 1.0,
) -> None:
    """レバレッジ上限を超える発注なら ``LeverageViolationError`` を送出する。

    ``check`` との違いは、判定結果を返すのではなく例外で止めること。
    発注パスではこちらを使い、呼び出し側が結果を無視できないようにする。

    Raises:
        LeverageViolationError: 上限を超える場合。
    """
    result = check(account, additional_notional, max_ratio)
    if not result.allowed:
        raise LeverageViolationError(result.reason)
