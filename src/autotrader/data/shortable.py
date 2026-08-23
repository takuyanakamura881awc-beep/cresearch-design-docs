"""売建可否と空売り価格規制の日次取得（安全装置 #12）。

【なぜ日次で取るのか】
一般信用（デイトレ）の売建可能銘柄は**在庫次第で日々変動する**。
制度信用で売建できない銘柄でも一般信用なら可能なものが多数あるが、
在庫は固定ではない。

前日の情報で発注すると弾かれる。ユニバース構築のハードフィルタ（D）と
発注前チェックの両方で使う。
"""

from __future__ import annotations

from datetime import date


def fetch_shortable_symbols(as_of: date) -> frozenset[str]:
    """指定日に一般信用（デイトレ）で売建可能な銘柄を取得する。"""
    raise NotImplementedError("Stage B（Phase 4）で実装する")


def fetch_price_restricted_symbols(as_of: date) -> frozenset[str]:
    """空売り価格規制がかかっている銘柄を取得する。

    直近公表価格から一定以上下落した銘柄が対象。発注が弾かれるため事前に除外する。
    """
    raise NotImplementedError("Stage B（Phase 4）で実装する")
