#!/usr/bin/env python3
"""TOPIX100 の細かい呼値が使えるか、銘柄数とコスト差を実測する。

    python scripts/measure_topix100.py --refresh   # 初回（ScaleCat を取りに行く）
    python scripts/measure_topix100.py             # 2回目以降（キャッシュを使う）

【なぜこれを見るのか】

これまでの3実験（竹・VWAP乖離単独・ギャップフェード）はすべて
**「優位 0〜16bps < コスト 15〜44bps」**で終わった。スプレッド想定を
0.5〜2.0本のどこに置いても結論は変わらない（意思決定ログ60）。

**残っている大きなレバーが呼値そのもの。** 東証の呼値は
TOPIX100（Core30 + Large70）だけ細かく、`autotrader.tick` に
テーブルは実装済みなのに**一度も使っていなかった**:

    900円の銘柄   通常 呼値1円   → 往復 22.2bps
                  TOPIX100 0.1円 → 往復  2.2bps   （10分の1）
    1,200円       通常 呼値1円   → 往復 16.7bps
                  TOPIX100 0.5円 → 往復  8.3bps   （半分）

**優位が10〜16bps あるので、この差は結論を変えうる。**

【何が制約になるか】

TOPIX100 は大型株で株価が高い。本プロジェクトの株価上限
**1,250円**（資金50万円 × 上限比率25% ÷ 単元100株）と両立するかは
未確認だった。`tick.py` の docstring は「ほとんど入ってこない」と
書いているが、**これは推測であって実測ではない**。

このスクリプトは推測を実測に置き換える:

- TOPIX100 構成銘柄が何銘柄あるか
- そのうち株価上限・流動性の条件を満たすのが何銘柄か
- 通過した銘柄の往復コストが通常銘柄と比べてどれだけ小さいか

【判定】

- **十分な数（監視枠が埋まる水準）が通る** → 既存の手法をこの銘柄群で
  再検定する価値がある。コストが1桁下がるので net の符号が変わりうる
- **数銘柄しか通らない** → 分散が効かず戦略として成立しない。
  この方向は打ち止め
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from autotrader.config import load_credentials, mask
from autotrader.data.base import (
    DataSourceError,
    EmptyResponseError,
    SubscriptionRangeError,
)
from autotrader.data.jquants import FREE_PLAN_DELAY_DAYS, JQuantsDataSource
from autotrader.data.store import BarStore
from autotrader.provenance import banner
from autotrader.risk.limits import DEFAULT_MAX_WEIGHT_PER_SYMBOL
from autotrader.risk.sizing import max_affordable_price
from autotrader.tick import DEFAULT_SPREAD_TICKS, spread_yen
from autotrader.types import Symbol

DATA_ROOT = Path("data")
UNIVERSE_PATH = DATA_ROOT / "universe.json"
MASTER_PATH = DATA_ROOT / "master_scale.json"
CAPITAL = Decimal(500_000)

MIN_TURNOVER_YEN = 300_000_000
"""流動性の下限（20日平均売買代金）。`docs/00` 意思決定ログ22 の実測値。"""

TURNOVER_DAYS = 20


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def round_trip_bps(price: float, *, topix100: bool) -> float:
    """往復コスト（bps）。スプレッド1本ぶんを払う前提。"""
    return float(spread_yen(price, DEFAULT_SPREAD_TICKS, topix100=topix100)) / price * 10_000.0


def save_master(symbols: tuple[Symbol, ...]) -> None:
    """規模区分つきの全銘柄一覧をキャッシュする。

    **`universe.json` とは別に持つ。** あちらは Layer 1 を通した後の
    銘柄だけなので、TOPIX100 が Layer 1 で落ちていると数えられない。
    """
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    MASTER_PATH.write_text(
        json.dumps(
            {
                "note": "規模区分（ScaleCat）つきの全銘柄。TOPIX100 判定に使う",
                "symbols": [
                    {
                        "code": s.code,
                        "name": s.name,
                        "market": s.market,
                        "margin_type": s.margin_type,
                        "sector": s.sector,
                        "scale_category": s.scale_category,
                    }
                    for s in symbols
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  保存: {MASTER_PATH}（{len(symbols)}銘柄）")


def fetch_master(source: JQuantsDataSource) -> tuple[Symbol, ...] | None:
    """規模区分つきの銘柄一覧を取る。**基準日を自分で決める必要がある。**

    `list_symbols` は ``as_of`` を必須にしている（現在の一覧を過去に
    適用するとサバイバーシップバイアスになるため）。契約範囲は
    400応答から自動学習されるので、**範囲外を叩いて学習させてから
    範囲内へ寄せる**のが一番少ないリクエストで済む。

    `fetch_bars.py` は日足を叩いて基準日を探しているが、こちらは
    銘柄一覧しか要らないので**日足を取りに行かない**（5件/分の制約下で
    リクエストを無駄にしない）。
    """
    probe = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS)
    for _ in range(12):
        while probe.weekday() >= 5:  # 土日は照会しない
            probe -= timedelta(days=1)
        try:
            symbols = source.list_symbols(probe)
        except SubscriptionRangeError as exc:
            if exc.covered_to is None:
                # **範囲を学習できていない。** 手探りで叩き続けない
                print(f"  NG: 契約範囲が判明しなかった — {exc}")
                return None
            # 範囲を学習できたので、その終端へ寄せて仕切り直す
            print(f"  契約範囲: {exc.covered_from} 〜 {exc.covered_to}")
            if probe <= exc.covered_to:
                # 既に範囲内なのに弾かれた＝これ以上寄せても同じ
                print("  NG: 範囲内のはずが照会できなかった")
                return None
            probe = exc.covered_to
            continue
        except EmptyResponseError:
            # 休日など。1日戻して再挑戦する
            probe -= timedelta(days=1)
            continue
        except DataSourceError as exc:
            print(f"  NG: {exc}")
            return None
        if symbols:
            print(f"  基準日: {probe}")
            return symbols
        probe -= timedelta(days=1)
    print("  NG: 基準日を決められなかった")
    return None


def load_master() -> tuple[Symbol, ...] | None:
    if not MASTER_PATH.is_file():
        return None
    payload = json.loads(MASTER_PATH.read_text(encoding="utf-8"))
    return tuple(
        Symbol(
            code=r["code"],
            name=r["name"],
            market=r.get("market"),
            margin_type=r.get("margin_type"),
            sector=r.get("sector"),
            scale_category=r.get("scale_category"),
        )
        for r in payload["symbols"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="J-Quants から銘柄一覧を取り直す（ScaleCat が要る。数分かかる）",
    )
    args = parser.parse_args()

    print("TOPIX100 の呼値が使えるかを実測する")
    print(banner())

    symbols = None if args.refresh else load_master()
    if symbols is None:
        if not args.refresh:
            print()
            print(f"  {MASTER_PATH} がない。初回は --refresh で取得する:")
            print("    python scripts/measure_topix100.py --refresh")
            return 1
        creds = load_credentials()
        if not creds.jquants_api_key:
            print("  JQUANTS_API_KEY がない（.env を確認する）")
            return 1
        print(f"  JQUANTS_API_KEY: {mask(creds.jquants_api_key)}")
        symbols = fetch_master(JQuantsDataSource(creds.jquants_api_key))
        if not symbols:
            return 1
        save_master(symbols)

    hr("1. 規模区分の内訳")
    counts: dict[str, int] = {}
    for s in symbols:
        key = s.scale_category or "(なし)"
        counts[key] = counts.get(key, 0) + 1
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {key:<24} {n:>5}")

    topix100 = tuple(s for s in symbols if s.is_topix100)
    print()
    print(f"  **TOPIX100（Core30 + Large70）: {len(topix100)}銘柄**")
    if not topix100:
        print("  → 判定できていない。ScaleCat の表記を上の内訳で確認する")
        return 1

    hr("2. 株価上限・流動性で絞る")
    ceiling = max_affordable_price(CAPITAL, DEFAULT_MAX_WEIGHT_PER_SYMBOL, 100)
    print(f"  株価上限: {ceiling:,}円"
          f"（資金{CAPITAL:,}円 × {DEFAULT_MAX_WEIGHT_PER_SYMBOL:.0%} ÷ 100株）")
    print(f"  流動性下限: {MIN_TURNOVER_YEN:,}円（{TURNOVER_DAYS}日平均売買代金）")
    print()

    store = BarStore(DATA_ROOT)
    passed: list[tuple[Symbol, float, float]] = []
    n_no_bars = 0
    n_too_expensive = 0
    n_too_thin = 0

    for s in topix100:
        bars = store.read(s.code, "1d")
        if not bars:
            n_no_bars += 1
            continue
        ordered = sorted(bars, key=lambda b: b.timestamp)
        price = ordered[-1].close
        if price <= 0:
            n_no_bars += 1
            continue
        if price > float(ceiling):
            n_too_expensive += 1
            continue
        recent = ordered[-TURNOVER_DAYS:]
        turnovers = [b.turnover for b in recent if b.turnover is not None]
        if not turnovers:
            # 売買代金がなければ 終値 × 出来高 で代用する
            turnovers = [b.close * b.volume for b in recent]
        turnover = statistics.fmean(turnovers)
        if turnover < MIN_TURNOVER_YEN:
            n_too_thin += 1
            continue
        passed.append((s, price, turnover))

    print(f"  日足なし          : {n_no_bars}")
    print(f"  株価が上限超え    : {n_too_expensive}")
    print(f"  流動性不足        : {n_too_thin}")
    print(f"  **通過            : {len(passed)}銘柄**")

    if not passed:
        print()
        print("  → **1銘柄も通らない。** TOPIX100 は株価が高く、")
        print("     資金50万円の上限と両立しない。この方向は打ち止め。")
        print("     （`tick.py` の docstring の見込みが実測で裏付けられた）")
        return 0

    hr("3. 通過した銘柄の往復コスト")
    print(f"  {'コード':<8}{'株価':>9}{'通常':>10}{'TOPIX100':>11}{'削減':>9}  銘柄名")
    print("  " + "-" * 68)
    regular_bps: list[float] = []
    fine_bps: list[float] = []
    for s, price, _turnover in sorted(passed, key=lambda x: x[1]):
        r = round_trip_bps(price, topix100=False)
        f = round_trip_bps(price, topix100=True)
        regular_bps.append(r)
        fine_bps.append(f)
        print(
            f"  {s.code:<8}{price:>8,.0f}円{r:>9.1f}b{f:>10.1f}b"
            f"{r / f:>8.1f}倍  {s.name[:18]}"
        )

    print()
    print(f"  往復コスト中央値: 通常 {statistics.median(regular_bps):.1f}bps"
          f" → TOPIX100 {statistics.median(fine_bps):.1f}bps")

    hr("4. 判定")
    print(f"  通過銘柄: {len(passed)}")
    print()
    if len(passed) >= 20:
        print("  → **十分な数が通る。** 既存の手法をこの銘柄群で再検定する価値がある。")
        print("     優位10〜16bps に対しコストが1桁下がるので net の符号が変わりうる。")
        print("     ただし TOPIX100 は大型株で ATR% が小さい傾向があるため、")
        print("     **優位そのものが小さくなる可能性**も同時に確かめること。")
    else:
        print("  → **数が足りない。** 分散が効かず戦略として成立しない")
        print("     （日次の監視枠50に対して少なすぎる）。")
        print("     コストが小さくても、1銘柄への集中は安全装置#7と両立しない。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
