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
from autotrader.data.yahoo import YahooDataSource
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

DAILY_LOOKBACK_DAYS = 730
"""日足を遡る暦日数。`scripts/fetch_bars.py` と同じ約2年。

yfinance の日足は期間による追加コストがほぼないので、
流動性の判定（20営業日）に必要な分より多めに取っておく。
"""


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


HISTORICAL_PATH = DATA_ROOT / "master_scale_historical.json"
"""**検証期間の開始時点**の規模区分。サバイバーシップバイアス対策。

`docs/03-universe.md` §4.2:「現在の一覧を過去に適用してはならない」。
今 TOPIX100 に入っている銘柄は「2年間 大型で居続けた勝ち組」なので、
その一覧で過去を測ると成績が構造的に過大評価される。
"""


def fetch_master_at(
    source: JQuantsDataSource, as_of: date, path: Path, label: str
) -> tuple[Symbol, ...] | None:
    """指定日時点の銘柄一覧を取って保存する。"""
    print(f"  {label}（{as_of}）を取得する")
    try:
        symbols = source.list_symbols(as_of)
    except SubscriptionRangeError as exc:
        print(f"  NG: 契約範囲外 — {exc}")
        return None
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return None
    if not symbols:
        print("  NG: 空の応答")
        return None
    _write_master(symbols, path, as_of, label)
    return symbols


def _write_master(
    symbols: tuple[Symbol, ...], path: Path, as_of: date, note: str
) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "as_of": as_of.isoformat(),
                "note": note,
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
    n_topix100 = sum(1 for s in symbols if s.is_topix100)
    print(f"    保存: {path}（{len(symbols)}銘柄 / TOPIX100 {n_topix100}）")


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


CAPITAL_SWEEP = (
    Decimal(500_000),
    Decimal(800_000),
    Decimal(1_200_000),
    Decimal(2_000_000),
    Decimal(3_000_000),
    Decimal(5_000_000),
)
"""資金をいくらにすれば何銘柄使えるようになるかを見るための刻み。"""


def _report_capital_sweep(
    topix100: tuple[Symbol, ...], store: BarStore
) -> None:
    """**何が制約なのかを分離する。**

    株価上限は市場の性質ではなく ``資金 × 1銘柄上限比率 ÷ 単元`` の
    逆算値（`docs/00` 意思決定ログ21）。TOPIX100 が使えないのが
    「呼値の話」なのか「資金の話」なのかは、資金を振ってみれば分かる。

    **これは増資を勧めるものではない。** 投入額は人間が決めること
    （`CLAUDE.md`）。ここで出すのは判断材料だけ。
    """
    prices: list[float] = []
    for s in topix100:
        bars = store.read(s.code, "1d")
        if not bars:
            continue
        price = sorted(bars, key=lambda b: b.timestamp)[-1].close
        if price > 0:
            prices.append(price)
    if not prices:
        return

    hr("3.5 資金をいくらにすれば何銘柄使えるか")
    print("  株価上限 = 資金 × 25%（安全装置#7）÷ 100株")
    print("  **市場の性質ではなく資金の制約**なので、資金を振れば動く。")
    print()
    print(f"  {'資金':>12}{'株価上限':>12}{'使える銘柄':>12}{'割合':>8}")
    print("  " + "-" * 46)
    for capital in CAPITAL_SWEEP:
        ceiling = float(max_affordable_price(capital, DEFAULT_MAX_WEIGHT_PER_SYMBOL, 100))
        usable = sum(1 for p in prices if p <= ceiling)
        marker = "  ← 現在" if capital == CAPITAL else ""
        print(
            f"  {int(capital):>11,}円{ceiling:>10,.0f}円"
            f"{usable:>10}銘柄{usable / len(prices):>7.0%}{marker}"
        )
    print()
    print(f"  （TOPIX100 のうち日足が取れた {len(prices)}銘柄が母数）")


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
        source = JQuantsDataSource(creds.jquants_api_key)
        symbols = fetch_master(source)
        if not symbols:
            return 1
        save_master(symbols)

        # **検証期間の開始時点の一覧も取る。** これがないと
        # 「今の勝ち組で過去を測る」サバイバーシップバイアスになる
        # （`docs/03-universe.md` §4.2）
        historical_as_of = date.today() - timedelta(days=DAILY_LOOKBACK_DAYS)
        fetch_master_at(
            source,
            historical_as_of,
            HISTORICAL_PATH,
            "検証期間の開始時点の規模区分（サバイバーシップ対策）",
        )

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

    # **日足を持っていない銘柄を先に取りに行く。**
    # `universe.json` は Layer 1 を通った銘柄だけなので、TOPIX100 の
    # 大半は日足を持っていない。それを「条件で落ちた」と数えると
    # **測定のアーティファクトを結論と取り違える**（実際に一度踏んだ）。
    missing = tuple(s.code for s in topix100 if not store.read(s.code, "1d"))
    if missing:
        print(f"  日足がない {len(missing)}銘柄を取得する（yfinance・日足は期間の追加コストなし）")
        yahoo = YahooDataSource()
        end = date.today()
        start = end - timedelta(days=DAILY_LOOKBACK_DAYS)
        try:
            fetched = yahoo.get_bars_batch(missing, "1d", start, end)
        except DataSourceError as exc:
            print(f"  NG: {exc}")
            return 1
        saved = 0
        for code, bars in fetched.items():
            if bars:
                store.write(code, "1d", bars)
                saved += 1
        print(f"    保存 {saved}銘柄 / 取得できず {len(missing) - saved}銘柄")

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

    _report_capital_sweep(topix100, store)

    hr("4. 判定")
    print(f"  通過銘柄: {len(passed)} / TOPIX100 {len(topix100)}銘柄")
    print()
    if n_no_bars > len(topix100) // 4:
        # **「データがない」を「条件で落ちた」と混同しない。**
        # 一度これで「4銘柄しか通らない」と誤読しかけた（意思決定ログ63）
        print(f"  → **判定不能。** {n_no_bars}銘柄の日足が取れていない。")
        print("     株価・流動性で落ちたのではなくデータ不足なので、")
        print("     この数字を「通らなかった」と読んではいけない。")
        print("     再実行して日足を揃えてから判定する。")
        return 1
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
