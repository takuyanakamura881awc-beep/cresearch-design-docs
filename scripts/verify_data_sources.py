#!/usr/bin/env python3
"""データ源の実測スクリプト。**Phase 1 の検証の中核。**

    python scripts/verify_data_sources.py

【なぜこのスクリプトが必要か】

設計時の想定（yfinance は1分足7日/5分足60日、J-Quants Free は12週遅延）は
**公開仕様に基づく未検証の値**。開発環境からは J-Quants・Yahoo の両方に
ネットワークが到達できず、実測できていない。

**推測のまま Phase 2 に進むと、期間の想定違いで詰まる。**
このスクリプトを実行し、出力を見てから次に進むこと。

【出力するもの】

1. J-Quants の疎通確認 — **失敗時は理由をそのまま表示する**
2. **レスポンスの実際の項目名** — V2 は項目名が短縮されており、正確な綴りは
   公開情報からの推定を含む。推測をコードに固定せず実データで確かめる
3. J-Quants Free の実際のデータ終端日（12週遅延の実測）
4. 日付指定の上場銘柄一覧（サバイバーシップ回避の要）
5. yfinance の実際の取得可能期間 — **境界の内側から測る**
6. 期間ズレ（yfinance 5分足と J-Quants 日足の間の穴）
7. **日足の突合検証**（J-Quants と yfinance の乖離率）
   → Light（1,650円/月）への課金判断の材料
8. yfinance のレート制限の挙動

APIキーは**マスクして表示する**。完全な値は出力しない。
"""

from __future__ import annotations

import sys
import traceback
from datetime import date, timedelta

from autotrader.config import load_credentials, mask
from autotrader.data.base import (
    DataSourceError,
    EmptyResponseError,
    RateLimitError,
    SubscriptionRangeError,
)
from autotrader.data.jquants import (
    ENDPOINT_DAILY_BARS,
    ENDPOINT_MASTER,
    FREE_PLAN_DELAY_DAYS,
    FREE_PLAN_REQUESTS_PER_MINUTE,
    JQuantsDataSource,
)
from autotrader.data.yahoo import MAX_LOOKBACK_DAYS, YahooDataSource

PROBE_SYMBOLS = ("7203", "8306", "9432")
"""実測に使う銘柄。流動性が高く上場廃止リスクの低い大型株を選ぶ。

トヨタ・三菱UFJ・NTT。データが取れないなら銘柄側ではなくAPI側の問題と判断できる。
"""


_WEEKDAYS = "月火水木金土日"


def fmt_date(d: date | None) -> str:
    """曜日つきで日付を表示する（``2026-05-29(金)``）。

    土日が絡む測定値は「ずれた」ように見えるため、曜日を明示して
    誤解を防ぐ。実測の遅延が86日と出たのは 5/31 が日曜だったため。

    ``None`` も受ける。呼び出し側は `SubscriptionRangeError.has_range` で
    絞り込んでいるが、**それはプロパティなので型検査では追えない**。
    ここで受けておけば、万一 ``None`` が来ても落ちずにそう表示される。
    """
    if d is None:
        return "(不明)"
    return f"{d.isoformat()}({_WEEKDAYS[d.weekday()]})"


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def check_credentials() -> str | None:
    hr("1. 認証情報")
    try:
        creds = load_credentials(require_kabus=False)
    except RuntimeError as exc:
        print(f"  NG: {exc}")
        return None
    print(f"  JQUANTS_API_KEY: {mask(creds.jquants_api_key)}")
    return creds.jquants_api_key


def make_jquants(api_key: str) -> JQuantsDataSource:
    """**スクリプト全体で1つだけ作る。**

    以前は関数ごとに作り直していたため、インスタンスごとに別のレートリミッタを
    持ち「自分はまだ1回も呼んでいない」と誤認して一斉に叩き、429 で弾かれた
    （突合検証の2銘柄目以降が失敗した原因）。
    """
    return JQuantsDataSource(api_key, requests_per_minute=FREE_PLAN_REQUESTS_PER_MINUTE)


def probe_jquants(source: JQuantsDataSource) -> None:
    """疎通を単独で確認する。**失敗理由をそのまま表示する。**

    **``code=`` で1銘柄だけ取る。** ``date=`` は1営業日で約4,200銘柄が返り、
    ページングで複数リクエストを消費する。5件/分の制約下では診断だけで枯渇する。
    （``date=`` 一括取得は Phase 2 の本番収集では正しい。用途が違う。）
    """
    hr("2. J-Quants — 疎通確認")
    end = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS + 7)
    start = end - timedelta(days=7)

    print(f"  エンドポイント: {ENDPOINT_DAILY_BARS}")
    print(f"  銘柄: {PROBE_SYMBOLS[0]}  期間: {start} 〜 {end}")
    print(f"  リクエスト間隔: {source.limiter_interval_seconds:.1f}秒/件")
    try:
        payload = source.get_raw(
            ENDPOINT_DAILY_BARS,
            {
                "code": PROBE_SYMBOLS[0],
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return

    print(f"  OK: 応答を受信（トップレベルのキー: {sorted(payload.keys())}）")
    _dump_record_keys(payload, "日足")


def _dump_record_keys(payload: dict[str, object], label: str) -> None:
    """レスポンスの**実際の項目名**を表示する。

    V2 は項目名が短縮されており（Open→O、Close→C など）、正確な綴りは
    公開情報からの推定を含む。**推測をコードに固定せず、実データで確かめる。**
    ここで出た項目名で data/jquants.py の _FIELD_CANDIDATES を整理する。
    """
    for key, value in payload.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"  {label}のレコード項目名（{key}）:")
            print(f"    {sorted(value[0].keys())}")
            print("  先頭レコードの中身:")
            for k, v in sorted(value[0].items()):
                print(f"    {k:>20} = {v!r}")
            return
    print(f"  ※ {label}のレコードが空だった（配列にデータなし）")


def check_jquants(source: JQuantsDataSource) -> date | None:
    """Free プランの実際のデータ終端日を調べる。

    【429 を「データなし」と混同しない】

    以前は ``DataSourceError`` を一括で捕捉して次の日付へ進んでいたため、
    レート制限で失敗した日を「その日はデータなし」と誤判定した。
    結果、実測の遅延が 84日から 88日にずれた（**測定値が嘘になった**）。

    ここでは ``RateLimitError`` を受けたら**測定を中断して測定不能と報告する**。
    誤った値を返すより、測定できなかったと言う方が安全。

    Returns:
        取得できた最新の営業日。失敗・測定不能なら None。
    """
    hr("3. J-Quants — 実際のデータ終端日")
    expected = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS)
    print(f"  想定終端日（12週=84日遅延）: {fmt_date(expected)}")
    print("  実測中（想定日から遡り、土日は飛ばす）...")

    probes = 0
    range_reported = False
    for back in range(FREE_PLAN_DELAY_DAYS - 3, FREE_PLAN_DELAY_DAYS + 15):
        probe = date.today() - timedelta(days=back)
        if probe.weekday() >= 5:
            continue  # 土日は必ず空振りする。リクエストの無駄

        probes += 1
        try:
            bars = source.get_bars(
                PROBE_SYMBOLS[0], "1d", probe, probe
            )
        except SubscriptionRangeError as exc:
            # 契約範囲外。以降は送信前に弾かれるので照会は増えない
            if exc.has_range and not range_reported:
                print(
                    f"  契約範囲: {fmt_date(exc.covered_from)} 〜 "
                    f"{fmt_date(exc.covered_to)}"
                )
                print("    （API が返した実際の範囲。以降は送信前に弾く）")
                range_reported = True
            continue
        except RateLimitError as exc:
            # データ不在と区別する。ここで continue すると測定が嘘になる
            print(f"  測定不能: レート制限に達した（{probes}件目の照会で中断）")
            print(f"    {exc}")
            print("    → 時間をおいて再実行するか、リクエスト間隔を広げること")
            return None
        except EmptyResponseError:
            continue  # この日はデータなし（正常な判定）
        except DataSourceError as exc:
            print(f"  {probe}: {exc}")
            continue

        if bars:
            delay = (date.today() - probe).days
            print(f"  OK: {fmt_date(probe)} のデータを取得（{probes}件の照会）")

            covered = source.subscription_range
            if covered is not None:
                print(f"  契約範囲の終端: {fmt_date(covered[1])}")
                spec_delay = (date.today() - covered[1]).days
                print(f"  仕様上の遅延  : {spec_delay}日（想定 {FREE_PLAN_DELAY_DAYS}日）")
                print(f"  最終営業日    : {fmt_date(probe)} → 実測 {delay}日")
                if covered[1].weekday() >= 5:
                    print("    ※ 契約範囲の終端が土日のため、実測は仕様より数日大きく出る")
            else:
                print(f"  実測の遅延: {delay}日（想定 {FREE_PLAN_DELAY_DAYS}日）")
                if abs(delay - FREE_PLAN_DELAY_DAYS) > 7:
                    print("  ※ 想定と1週間以上ずれている。定数の見直しを検討")
            return probe

    print(f"  NG: {probes}件照会したがデータを取得できなかった")
    return None


def check_jquants_symbols(source: JQuantsDataSource, as_of: date) -> None:
    hr("4. J-Quants — 日付指定の上場銘柄一覧（サバイバーシップ回避の要）")
    try:
        raw = source.get_raw(ENDPOINT_MASTER, {"date": as_of.isoformat()})
        _dump_record_keys(raw, "銘柄一覧")
        symbols = source.list_symbols(as_of)
    except SubscriptionRangeError as exc:
        print(f"  NG: {exc}")
        return
    except RateLimitError as exc:
        print(f"  測定不能: レート制限 — {exc}")
        print("    → 銘柄一覧は全銘柄を返すためページングでリクエストを消費する")
        return
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return
    if not symbols:
        print("  NG: 空だった")
        return
    print(f"  OK: {as_of} 時点で {len(symbols)}銘柄")
    for sym in symbols[:3]:
        print(
            f"    {sym.code} {sym.name} / 市場={sym.market}"
            f" / 信用={sym.margin_type} / 業種={sym.sector}"
        )

    # ユニバース構築のフィルタが代理指標なしで実装できるかを確認する
    prime = [s for s in symbols if s.market == "プライム"]
    loanable = [s for s in symbols if s.margin_type == "貸借"]
    both = [s for s in symbols if s.market == "プライム" and s.margin_type == "貸借"]
    print()
    print(f"  プライム            : {len(prime)}銘柄  （フィルタA）")
    print(f"  貸借銘柄（売建可）  : {len(loanable)}銘柄  （フィルタD）")
    print(f"  両方を満たす        : {len(both)}銘柄  ← Layer 1 の出発点")


def check_yahoo_lookback() -> dict[str, int]:
    """yfinance が実際に何日遡れるかを足ごとに実測する。"""
    hr("5. yfinance — 実際の取得可能期間")
    source = YahooDataSource()
    measured: dict[str, int] = {}

    print("  （境界の内側から試し、成功した最大値と失敗した最小値で上限を挟み込む）")
    print()

    for interval, expected in sorted(MAX_LOOKBACK_DAYS.items(), key=lambda kv: kv[1]):
        if interval not in ("1m", "5m", "60m"):
            continue  # 代表的な足だけ測る（リクエスト数を抑える）
        actual, failed_at = _measure_lookback(source, interval, expected)
        measured[interval] = actual
        flag = "OK" if actual >= expected - 2 else "※想定より短い"
        failure = f" / {failed_at}日は失敗" if failed_at is not None else ""
        print(
            f"  {interval:>4}: 成功 {actual:>3}日{failure}"
            f"  / 想定 {expected:>3}日  {flag}"
        )

    # 日足は制限がないはず
    actual_daily, _ = _measure_lookback(source, "1d", 400)
    print(f"  {'1d':>4}: 成功 {actual_daily:>3}日以上（制限なしのはず）")

    print()
    print("  ※ 想定と食い違う場合は data/yahoo.py の MAX_LOOKBACK_DAYS を実測値に修正する")
    return measured


def _spans_weekday(start: date, end: date) -> bool:
    """期間内に平日が1日でもあるか。"""
    day = start
    while day <= end:
        if day.weekday() < 5:
            return True
        day += timedelta(days=1)
    return False


def _try_fetch(source: YahooDataSource, interval: str, days: int) -> bool:
    """指定日数ぶん遡って取得できるか試す。

    **開始日は「今日」から数える。終端をずらしてはならない。**

    Yahoo の「直近60日以内」という判定は**今日を基準**にしている。
    終端を直前の平日に寄せると、同じ日数指定でも開始日がその分古くなり、
    上限に引っかかって実測値が短く出る。

    実際にこれを踏んだ。日曜に実行したところ 5m が 58日→56日、
    60m が 728日→726日 と、すべて2日ぶん短く測定された
    （終端を金曜に寄せたため開始日が2日古くなった）。

    土日に短い期間を指定すると平日が1日も含まれず空振りするが、
    それは上限とは無関係なので `_measure_lookback` 側で除外する。
    """
    today = date.today()
    try:
        bars = source.get_bars_batch(
            (PROBE_SYMBOLS[0],), interval, today - timedelta(days=days), today
        )
    except DataSourceError:
        return False
    return bool(bars.get(PROBE_SYMBOLS[0]))


def _measure_lookback(
    source: YahooDataSource, interval: str, hint: int
) -> tuple[int, int | None]:
    """遡れる日数を実測する。

    **境界ちょうどを試さない。** 以前は上限そのもの（60日など）から試していたため、
    Yahoo 側の「直近60日以内」の判定に触れて弾かれ、実測値が常に0になっていた。

        5m data not available ... The requested range must be within the last 60 days.

    内側から始めて、成功した最大値と失敗した最小値の**両方**を返す。
    こうすれば上限を挟み込める（「60日は成功、62日は失敗」）。

    Returns:
        (成功した最大日数, 失敗した最小日数)。後者は見つからなければ ``None``。
    """
    # 境界の内側から、少しずつ外へ
    today = date.today()
    candidates = sorted({max(1, hint - 2), hint // 2, hint // 4, 5, hint, hint + 2})
    # 平日を含まない期間は、上限とは無関係に空振りする（土日に実行した場合）
    candidates = [
        d for d in candidates if _spans_weekday(today - timedelta(days=d), today)
    ]

    best = 0
    smallest_failure: int | None = None
    for days in candidates:
        if _try_fetch(source, interval, days):
            best = max(best, days)
        elif smallest_failure is None or days < smallest_failure:
            smallest_failure = days

    return best, smallest_failure


def check_gap(jquants_end: date | None, yahoo_5m_days: int) -> None:
    """5分足と日足の期間が重なるかを実測値から判定する。"""
    hr("6. 期間ズレ — 5分足と日足が重なるか")

    if jquants_end is None:
        print("  判定不能（J-Quants のデータ終端日が取れていない）")
        return

    yahoo_5m_start = date.today() - timedelta(days=yahoo_5m_days or 60)
    print(f"  J-Quants 日足の終端 : {fmt_date(jquants_end)}")
    print(f"  yfinance 5分足の始端: {fmt_date(yahoo_5m_start)}")

    gap = (yahoo_5m_start - jquants_end).days
    if gap > 0:
        print()
        print(f"  → 重ならない。間に {gap}日の穴がある")
        print("     竹の検証には5分足と同じ期間の日足が要る（ATR%・売買代金・出来高比）")
        print("     この期間の日足は yfinance で補完する必要がある")
        print("     ※ 両端とも毎日1日ずつ進むため、この穴の幅はほぼ一定。時間では埋まらない")
    else:
        print()
        print(f"  → {-gap}日ぶん重なっている。J-Quants の日足だけで賄える")


def compare_daily(source: JQuantsDataSource, jquants_end: date | None) -> None:
    """J-Quants と yfinance の日足を突き合わせ、**調整基準の違いを切り分ける**。

    前回の実測で 7203 の乖離が「中央値1.467% / 最大1.467%」と一致した。
    中央値と最大値が同じなのはランダムなノイズではなく**一定のオフセット**の証拠で、
    配当調整の有無が原因と考えられる（yfinance の auto_adjust は配当も調整する）。

    ここでは3通りを並べて、どれが J-Quants の AdjC と一致するかを実データで確定させる:

    - yfinance 配当調整なし（既定。分割のみ調整）← これが一致するはず
    - yfinance 配当調整あり（auto_adjust=True）
    - J-Quants の生値 C（無調整）

    **Light（1,650円/月）へ課金すべきかの判断は、この切り分けの後で行う。**
    基準の違いによる差を「品質の差」と誤認して課金しない。
    """
    hr("7. 日足の突合検証 — 調整基準の切り分け")

    if jquants_end is None:
        print("  判定不能（J-Quants のデータ終端日が取れていない）")
        return

    end = jquants_end
    start = end - timedelta(days=180)
    symbol = PROBE_SYMBOLS[0]  # レート制限を使い切らないよう1銘柄に絞る

    try:
        jq_bars = source.get_bars(symbol, "1d", start, end)
    except RateLimitError as exc:
        print(f"  測定不能: レート制限 — {exc}")
        return
    except DataSourceError as exc:
        print(f"  {symbol}: J-Quants 取得失敗 — {exc}")
        return

    jq_adj = {b.timestamp.date(): b.close for b in jq_bars}

    variants: list[tuple[str, dict[date, float]]] = []
    for label, adjust in (("配当調整なし", False), ("配当調整あり", True)):
        try:
            yh_bars = YahooDataSource(adjust_dividends=adjust).get_bars(
                symbol, "1d", start, end
            )
        except DataSourceError as exc:
            print(f"  yfinance({label}): 取得失敗 — {exc}")
            continue
        variants.append((label, {b.timestamp.date(): b.close for b in yh_bars}))

    print(f"  銘柄: {symbol}  期間: {start} 〜 {end}")
    print()
    for label, yh_map in variants:
        _report_deviation(f"J-Quants AdjC vs yfinance {label}", jq_adj, yh_map)

    print()
    print("  【読み方】")
    print("   配当調整なしの乖離が十分小さい → 基準が揃った。Free のままでよい")
    print("   どちらも乖離が大きい → 分割調整そのものがずれている。Light を検討")


def _report_deviation(
    label: str, base: dict[date, float], other: dict[date, float]
) -> None:
    """2系列の乖離率を要約する。"""
    common = sorted(set(base) & set(other))
    if not common:
        print(f"  {label}: 共通する日付がない（比較不能）")
        return

    diffs = sorted(abs(base[d] - other[d]) / base[d] for d in common if base[d])
    if not diffs:
        print(f"  {label}: 比較可能な値がない")
        return

    median = diffs[len(diffs) // 2]
    over_1pct = sum(1 for d in diffs if d > 0.01)
    print(
        f"  {label}:\n"
        f"    共通{len(common)}日  中央値 {median * 100:.4f}%  "
        f"最大 {diffs[-1] * 100:.4f}%  1%超 {over_1pct}日"
    )


def check_yahoo_rate_limit() -> None:
    """バッチ取得でブロックされないかを確認する。"""
    hr("8. yfinance — レート制限の挙動")
    source = YahooDataSource()
    today = date.today()
    try:
        bars = source.get_bars_batch(
            PROBE_SYMBOLS, "1d", today - timedelta(days=30), today
        )
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        print("  → レート制限にかかっている可能性。時間をおいて再実行すること")
        return

    got = len(bars)
    print(f"  {len(PROBE_SYMBOLS)}銘柄中 {got}銘柄を取得")
    if got < len(PROBE_SYMBOLS):
        missing = [s for s in PROBE_SYMBOLS if s not in bars]
        print(f"  ※ 取得できなかった銘柄: {', '.join(missing)}")
        print("     大型株で取れないのはブロックの可能性が高い")
    else:
        print("  OK: 全銘柄取得できた")


def main() -> int:
    print("データ源の実測 — Phase 1 の検証")
    print(f"実行日: {date.today()}")

    api_key = check_credentials()
    if api_key is None:
        print("\n認証情報がないため中断する。.env を設定すること")
        return 1

    # レートリミッタを共有するため、スクリプト全体で1つだけ作る
    jq = make_jquants(api_key)

    try:
        probe_jquants(jq)
    except Exception:  # noqa: BLE001 - 実測スクリプトなので握らず全部見せる
        traceback.print_exc()

    jquants_end: date | None = None
    try:
        jquants_end = check_jquants(jq)
        if jquants_end is not None:
            check_jquants_symbols(jq, jquants_end)
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    measured: dict[str, int] = {}
    try:
        measured = check_yahoo_lookback()
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    try:
        check_gap(jquants_end, measured.get("5m", 0))
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    if jquants_end:
        try:
            compare_daily(jq, jquants_end)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    try:
        check_yahoo_rate_limit()
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    hr("完了")
    print("  この出力を確認してから Phase 2（ユニバース構築）に進むこと。")
    print("  想定と食い違う値があれば、対応する定数を実測値に修正する。")
    print()
    print("  ※ 「測定不能: レート制限」が出た場合、その項目の値は不明であって")
    print("     「データがない」ではない。時間をおいて再実行すること。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
