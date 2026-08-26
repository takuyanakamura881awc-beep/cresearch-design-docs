#!/usr/bin/env python3
"""実データのバックテストに要るバーを取得してキャッシュする。

    python scripts/fetch_bars.py              # 足りないぶんだけ取る
    python scripts/fetch_bars.py --refresh    # ユニバースから取り直す

【3段構え】

===========  ==============  ====================================================
段           データ源        用途
===========  ==============  ====================================================
1. 銘柄一覧  J-Quants        市場区分・信用区分（**yfinance では取れない**）
2. 日足      yfinance        Layer 2 の日次選定（ATR%・売買代金・株価）
3. 5分足     yfinance        約定のシミュレーション（58日ぶん）
===========  ==============  ====================================================

段1だけ J-Quants で、しかも**1回で済む**（結果を ``data/universe.json`` に保存）。
5件/分の制約下で8〜12分かかるので、毎回は叩かない。

【Stage A の既知の制約 — 継ぎ目のズレ】

J-Quants Free の終端は2026-05-31、yfinance の5分足は58日ぶん。
**ユニバースを選んだ日と5分足のある期間が約28日ずれる**（docs/09 §2.4）。

段1の銘柄リストは「5/29 時点で Layer 1 を通った133銘柄」で、
検証期間（6月末〜8月）から見ると**1ヶ月古い**。
ルックアヘッドではない（過去の情報を未来に適用しているだけ）が、
本来の Layer 2 は日次で選び直す。

そこで**段2の日足は検証期間内のぶんを取り、日次の選定はそちらで回す**。
静的属性（市場・信用区分）だけ段1のものを使う。
この制約は有料プランか Stage B で解消する。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from autotrader.config import load_credentials, mask
from autotrader.data.base import DataSourceError, RateLimitError
from autotrader.data.jquants import JQuantsDataSource
from autotrader.data.store import BarStore
from autotrader.data.yahoo import MAX_LOOKBACK_DAYS, YahooDataSource
from autotrader.types import Bar, Symbol
from autotrader.universe.builder import build
from autotrader.universe.filters import FilterConfig

DATA_ROOT = Path("data")
UNIVERSE_PATH = DATA_ROOT / "universe.json"
TURNOVER_DAYS = 20

DAILY_LOOKBACK_DAYS = 730
"""日足を遡る暦日数。

**Layer 2 の指標そのものは20営業日ぶんで足りる**（`SelectorConfig.min_bars`）。
5分足の期間の先頭よりさらに前から確保しないと、検証初日に選定できない、
というのが本来の下限。

**それより大きく（約2年）取っているのは `measure_gap_fade.py` のため。**
120日（約85営業日）のままでは、日足診断の強み（J-Quants無料の
2年分という桁違いの母数）を活かせない——実測でも1銘柄あたり約78日
しか貯まっておらず、5分足の検証窓（39〜80営業日）に対して2倍程度の
優位しかなかった。yfinance の日足取得は期間による追加コストが
ほぼない（`docs/09-data-sources.md` §2.1: 1d=制限なし）ので、
毎週の取得コストを増やさずに済む。
"""


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
# 段1: 銘柄一覧（J-Quants）
# ---------------------------------------------------------------------------


def load_universe() -> tuple[Symbol, ...] | None:
    """保存済みの銘柄一覧を読む。無ければ ``None``。"""
    if not UNIVERSE_PATH.is_file():
        return None
    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return tuple(
        Symbol(
            code=row["code"],
            name=row["name"],
            market=row.get("market"),
            margin_type=row.get("margin_type"),
            sector=row.get("sector"),
        )
        for row in payload["symbols"]
    )


def save_universe(as_of: date, symbols: tuple[Symbol, ...]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    UNIVERSE_PATH.write_text(
        json.dumps(
            {
                "as_of": as_of.isoformat(),
                "note": "J-Quants の Layer 1 通過銘柄。市場区分と信用区分の取得元",
                "symbols": [
                    {
                        "code": s.code,
                        "name": s.name,
                        "market": s.market,
                        "margin_type": s.margin_type,
                        "sector": s.sector,
                    }
                    for s in symbols
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  保存: {UNIVERSE_PATH}（{len(symbols)}銘柄 / 基準日 {as_of}）")


def fetch_universe(source: JQuantsDataSource) -> tuple[Symbol, ...] | None:
    """J-Quants から Layer 1 を通した銘柄一覧を作る。**8〜12分かかる。**"""
    print("  契約範囲内の最新営業日を探す")
    as_of: date | None = None
    for back in range(80, 100):
        probe = date.today() - timedelta(days=back)
        if probe.weekday() >= 5:
            continue
        try:
            if source.get_bars_for_date(probe):
                as_of = probe
                break
        except DataSourceError:
            continue
    if as_of is None:
        print("  NG: 基準日を決められなかった")
        return None
    print(f"  基準日: {as_of}")

    print(f"  日足を{TURNOVER_DAYS}営業日ぶん収集する（12秒間隔。8〜12分）")
    collected: dict[str, list[Bar]] = {}
    found, probe, attempts = 0, as_of, 0
    while found < TURNOVER_DAYS and attempts < TURNOVER_DAYS * 2:
        attempts += 1
        if probe.weekday() >= 5:
            probe -= timedelta(days=1)
            continue
        try:
            day_bars = source.get_bars_for_date(probe)
        except RateLimitError as exc:
            print(f"  中断: レート制限 — {exc}")
            return None
        except DataSourceError:
            probe -= timedelta(days=1)
            continue
        for code, bars in day_bars.items():
            collected.setdefault(code, []).extend(bars)
        found += 1
        print(f"    {probe}: {len(day_bars):>5}銘柄  ({found}/{TURNOVER_DAYS})")
        probe -= timedelta(days=1)

    bars_by_symbol = {
        code: tuple(sorted(bars, key=lambda b: b.timestamp))
        for code, bars in collected.items()
    }
    listed = source.list_symbols(as_of)
    if listed is None:
        print("  NG: 銘柄一覧を取得できなかった")
        return None

    snapshot = build(
        as_of, source, FilterConfig(), bars_by_symbol=bars_by_symbol, symbols=listed
    )
    by_code = {s.code: s for s in listed}
    passed = tuple(by_code[r.symbol] for r in snapshot.passed if r.symbol in by_code)
    print(f"  Layer 1 通過: {len(passed)}銘柄")
    save_universe(as_of, passed)
    return passed


# ---------------------------------------------------------------------------
# 段2・段3: バー（yfinance）
# ---------------------------------------------------------------------------


def fetch_interval(
    source: YahooDataSource,
    store: BarStore,
    symbols: tuple[Symbol, ...],
    interval: str,
    start: date,
    end: date,
) -> tuple[int, list[str]]:
    """指定の足を取得して保存する。

    Returns:
        (保存できた銘柄数, 取れなかった銘柄コード)。

        **取れなかった銘柄を黙って捨てない。** 上場廃止・ティッカー変更で
        欠けることがあり、気づかず母集団から抜けると成績が過大評価になる。
    """
    codes = tuple(s.code for s in symbols)
    print(f"  {interval}: {len(codes)}銘柄 × {start} 〜 {end}")

    try:
        fetched = source.get_bars_batch(codes, interval, start, end)
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return 0, list(codes)

    saved, missing = 0, []
    for code in codes:
        bars = fetched.get(code, ())
        if not bars:
            missing.append(code)
            continue
        store.write(code, interval, bars)
        store.record_fetch(code, interval, start, end, source.name, len(bars))
        saved += 1

    print(f"    保存 {saved}銘柄 / 取得できず {len(missing)}銘柄")
    if missing:
        print(f"    取得できなかった銘柄: {', '.join(missing[:10])}"
              + (f" ほか{len(missing) - 10}件" if len(missing) > 10 else ""))
    return saved, missing


WALKFORWARD_DAYS = 80
"""ウォークフォワードに要る営業日数の目安（学習60 + 検証20）。"""


def _print_accumulation(store: BarStore, symbols: tuple[Symbol, ...]) -> None:
    """5分足の蓄積状況を出す。

    【なぜ蓄積が要るか】

    **yfinance の5分足は常に58日ぶんしか返さない。** 一度の取得では
    ウォークフォワード（学習60 + 検証20 = 80営業日）に足りない。

    ただし `BarStore.write` は既存データとマージするので、
    **定期実行すれば58日の窓を超えて溜まっていく。**
    週1回まわせば3ヶ月後には100営業日前後になる。

    ここで進捗を出すのは、「あと何日で Phase 4 が回せるか」を
    毎回の実行で確認できるようにするため。
    """
    hr("5. 5分足の蓄積状況")

    days: set[date] = set()
    covered = 0
    for symbol in symbols:
        span = store.coverage(symbol.code, "5m")
        if span is None:
            continue
        covered += 1
        for bar in store.read(symbol.code, "5m"):
            days.add(bar.timestamp.date())

    if not days:
        print("  5分足がまだ無い")
        return

    business_days = len(days)
    print(f"  期間      : {min(days)} 〜 {max(days)}")
    print(f"  営業日数  : {business_days}日（{covered}銘柄）")
    print(f"  必要日数  : {WALKFORWARD_DAYS}日（ウォークフォワード 学習60 + 検証20）")

    if business_days >= WALKFORWARD_DAYS:
        print("  **Phase 4（ウォークフォワードでのパラメータ確定）を回せる**")
    else:
        remaining = WALKFORWARD_DAYS - business_days
        print(f"  あと {remaining}営業日ぶん足りない（約{remaining / 5:.0f}週）")
        print()
        print("  **yfinance の5分足は常に58日ぶんしか返さない。**")
        print("  BarStore は既存データとマージするので、週1回この取得を回せば")
        print("  58日の窓を超えて溜まっていく。定期実行を習慣にすること。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="銘柄一覧を J-Quants から取り直す（8〜12分かかる）",
    )
    args = parser.parse_args()

    print("実データのバーを取得する")
    print(f"実行日: {date.today()}")

    hr("1. 銘柄一覧（市場区分・信用区分）")
    symbols = None if args.refresh else load_universe()
    if symbols is None:
        try:
            creds = load_credentials(require_kabus=False)
        except RuntimeError as exc:
            print(f"  NG: {exc}")
            return 1
        print(f"  JQUANTS_API_KEY: {mask(creds.jquants_api_key)}")
        symbols = fetch_universe(JQuantsDataSource(creds.jquants_api_key))
        if symbols is None:
            return 1
    else:
        print(f"  キャッシュを使う: {UNIVERSE_PATH}（{len(symbols)}銘柄）")
        print("  取り直すには --refresh")

    store = BarStore(DATA_ROOT)
    yahoo = YahooDataSource()
    today = date.today()

    hr("2. 日足（Layer 2 の日次選定に使う）")
    daily_start = today - timedelta(days=DAILY_LOOKBACK_DAYS)
    _, missing_daily = fetch_interval(yahoo, store, symbols, "1d", daily_start, today)

    hr("3. 5分足（約定のシミュレーションに使う）")
    lookback = MAX_LOOKBACK_DAYS["5m"]
    intraday_start = today - timedelta(days=lookback)
    print(f"  yfinance の5分足は{lookback}日ぶんが上限（実測で確定）")
    saved_5m, missing_5m = fetch_interval(
        yahoo, store, symbols, "5m", intraday_start, today
    )

    hr("4. 結果")
    print(f"  日足   : {len(symbols) - len(missing_daily)}/{len(symbols)}銘柄")
    print(f"  5分足  : {saved_5m}/{len(symbols)}銘柄")
    print(f"  保存先 : {DATA_ROOT.resolve()}")

    both = [
        s.code
        for s in symbols
        if s.code not in missing_5m and s.code not in missing_daily
    ]
    print(f"  **両方そろった: {len(both)}銘柄**")

    # **上場廃止と yfinance 側の欠損を切り分ける。**
    # 日足も5分足も取れないなら上場廃止・ティッカー変更を疑う。
    # 日足は取れて5分足だけ取れないなら、その銘柄が存在しないのではなく
    # 分足の配信がないだけ（出来高が薄い時間帯が続くと起きる）。
    gone = sorted(set(missing_5m) & set(missing_daily))
    intraday_only = sorted(set(missing_5m) - set(missing_daily))
    if gone:
        print()
        print(f"  日足も5分足も取れない（上場廃止・ティッカー変更を疑う）: {len(gone)}銘柄")
        print(f"    {', '.join(gone[:10])}")
        print("    → data/universe.json から外すことを検討する")
    if intraday_only:
        print()
        print(f"  日足は取れるが5分足だけ取れない: {len(intraday_only)}銘柄")
        print(f"    {', '.join(intraday_only[:10])}")
        print("    → 上場はしている。yfinance 側の分足欠損。検証からは外れる")

    if not both:
        print()
        print("  NG: 検証できる銘柄がない")
        return 1

    _print_accumulation(store, symbols)

    print()
    print("  次: python scripts/backtest_take.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
