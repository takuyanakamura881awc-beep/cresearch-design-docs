#!/usr/bin/env python3
"""スプレッドが呼値の何本ぶんかを、手元の日足から推定する。

    python scripts/measure_spread.py

事前に ``python scripts/fetch_bars.py`` で日足を貯めておく
（新規のネットワーク取得は行わない）。

【何を確かめるのか】

``tick.DEFAULT_SPREAD_TICKS = 2.0`` は **このプロジェクト最大の当て推量**。
Stage A では板が取れないので直接は測れないが、**この1つの数値が
全コスト計算を線形にスケールする**——往復コスト、`min_atr_yen`、
Layer 2 のコストスコア、バックテストの約定価格、損益分岐の勝率。

これまでの3実験（竹・VWAP乖離単独・ギャップフェード）はすべて
「gross は小さく正、コストがそれを上回って net 負」で終わっている。
**この構図が「優位が本当にない」のか「コストを過大に見積もっている」
のかは、スプレッドが分からないと切り分けられない。**

日足の高安だけから実効スプレッドを推定する Corwin-Schultz（2012）を
`autotrader.spread` に実装してあるので、それを全銘柄に当てて
**呼値（`autotrader.tick.tick_size`）の何本ぶんか**に換算する。

【この結果をどう使うか】

- **2本前後** → 現行の想定は妥当。コストは過大評価ではなく、
  これまでの net 負は本物。手法探しを続けるより、コスト構造そのもの
  （保有期間を延ばす・より高い株価帯）を見直す段階
- **1本前後** → コストを倍に見積もっていたことになる。
  **既存の全実験を再評価する必要がある**（gross は変わらないが net は変わる）
- **3本以上** → さらに厳しい。デイトレの往復では勝ち目が薄いことの傍証

**推定値は下限として扱う。** Corwin-Schultz は取引がまばらな銘柄ほど
下振れする（`autotrader.spread` の docstring 参照）。
「思ったより狭かった」という結果が出ても、**楽観側には倒さない**。
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.provenance import banner
from autotrader.spread import corwin_schultz, corwin_schultz_pooled
from autotrader.tick import DEFAULT_SPREAD_TICKS, tick_size
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

MIN_PAIRS = 100
"""推定に使う最低ペア数。**これ未満の銘柄は結果に混ぜない。**

日数が少ないと β・γ の平均が安定せず、推定が大きく振れる。
"""


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def load_symbols() -> tuple[Symbol, ...]:
    """`scripts/backtest_take.py` の同名関数と同じ読み込み。

    **重複させている。** スクリプトファイルは pythonpath に乗らないため、
    スクリプト間で import せず、それぞれ自己完結させる。
    """
    import json

    path = DATA_ROOT / "universe.json"
    if not path.is_file():
        raise SystemExit(f"{path} がない。先に python scripts/fetch_bars.py を実行する")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Symbol(
            code=r["code"],
            name=r["name"],
            market=r.get("market"),
            margin_type=r.get("margin_type"),
            sector=r.get("sector"),
        )
        for r in payload["symbols"]
    )


def main() -> int:
    print("スプレッドを日足から推定する（Corwin-Schultz 2012）")
    print(banner())

    symbols = load_symbols()
    store = BarStore(DATA_ROOT)

    daily: dict[str, tuple[Bar, ...]] = {}
    prices: list[float] = []
    per_symbol_bps: list[float] = []
    n_negative = 0
    n_short = 0

    for symbol in symbols:
        bars = store.read(symbol.code, "1d")
        if not bars:
            continue
        estimate = corwin_schultz(bars)
        if estimate is None:
            continue
        if estimate.n_pairs < MIN_PAIRS:
            n_short += 1
            continue

        daily[symbol.code] = bars
        # **負も含めて集める。** 正だけ拾うと上振れする（下記）
        per_symbol_bps.append(estimate.spread_bps)
        if not estimate.usable:
            n_negative += 1
        price = sorted(bars, key=lambda b: b.timestamp)[-1].close
        if price > 0:
            prices.append(price)

    hr("結果")
    print(f"  対象銘柄: {len(daily)}（日数不足で除外: {n_short}）")

    if not daily or not prices:
        print()
        print("  **推定できる銘柄がない。** 先に日足を貯める:")
        print("    python scripts/fetch_bars.py")
        return 1

    pooled = corwin_schultz_pooled(daily)
    if pooled is None:
        print("  **推定できなかった。** 使えるペアが1つもない")
        return 1

    median_price = statistics.median(prices)
    pooled_ticks = pooled.spread_pct * median_price / float(tick_size(median_price))

    print()
    print("  【全銘柄をまとめた推定（これを見る）】")
    print(f"    ペア数        : {pooled.n_pairs:,}")
    print(f"    スプレッド    : {pooled.spread_bps:.1f}bps")
    print(f"    呼値の本数    : {pooled_ticks:.2f}本   （代表株価 {median_price:,.0f}円）")

    print()
    print("  【参考: 銘柄ごとに推定した場合】")
    print(f"    負の推定値    : {n_negative}/{len(per_symbol_bps)}銘柄")
    positive = [v for v in per_symbol_bps if v > 0]
    if positive:
        print(
            f"    正のみの中央値: {statistics.median(positive):.1f}bps"
            "  ← **上振れする。使わない**"
        )
    print("    **負を捨てて正だけ平均すると、ノイズで上振れした銘柄だけが残る。**")
    print("    合成データでは真0bpsでも「正のみ」は+4.8bpsを返した。")
    print("    負の割合そのものは、推定がノイズ床にどれだけ近いかの目安になる。")

    print()
    print(f"  **現行の想定: {DEFAULT_SPREAD_TICKS:.1f}本** / 推定: {pooled_ticks:.2f}本")
    print()
    if not pooled.usable:
        print("  → **まとめても負。** この銘柄群では推定が効いていない。")
        print("     想定を動かす根拠にはできない。")
    elif pooled_ticks < DEFAULT_SPREAD_TICKS * 0.75:
        print("  → 想定より**狭い**。コストを過大に見積もっていた可能性がある。")
        print("     既存の実験は gross は変わらないが net が変わるので再評価が要る。")
        print("     **ただし推定は下振れする側。鵜呑みにして楽観側へ倒さない。**")
        print("     既定値は変えず、まず --spread-ticks で感度を見る。")
    elif pooled_ticks > DEFAULT_SPREAD_TICKS * 1.25:
        print("  → 想定より**広い**。コストはさらに重く、デイトレの往復は不利。")
        print("     保有期間を延ばすか、コスト構造そのものを見直す段階。")
    else:
        print("  → 現行の想定は**妥当**。これまでの net 負はコストの過大評価ではない。")
        print("     手法を探し続けるより、コスト構造（保有期間・株価帯）を見直す。")

    print()
    print("  **これは推定であって実測ではない。** 取引がまばらな銘柄ほど")
    print("  下振れする（`autotrader.spread` 参照）。板を録れるように")
    print("  なったら（楽天 マーケットスピード II RSS 等）上書きする。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
