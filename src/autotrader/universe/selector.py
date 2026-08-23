"""Layer 2: 日次銘柄選定。

Layer 1 を通過した母集団から、**その日実際に監視する50銘柄**を選ぶ。

【上限50の由来】
Stage B の WebSocket リアルタイム配信の制限。
Stage A（ヒストリカル検証）には技術的な制約はないが、**同じ50に揃える**。
銘柄数を変えると成績が変わり、Stage A の検証結果が Stage B に引き継げなくなるため。

【重要】ルックアヘッドバイアスの回避（docs/03-universe.md §4.3）

寄り前の選定に使ってよいのは、**前日大引けまでの確定情報と
（Stage B のみ）当日の寄り前気配だけ**。
当日の終値や日中データを使うと、バックテストだけ好成績になり本番で再現しない。

**「気をつける」では防げないので、構造で防ぐ。**
指標の計算は `build_candidates` を必ず経由し、そこで
``timestamp.date() < trade_date`` を満たすバーだけに絞ってから
`compute_features` に渡す。当日のバーは関数の入口で捨てられるので、
指標側から未来を覗く経路が存在しない。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import median

from autotrader.risk.limits import max_atr_pct
from autotrader.types import Bar, PriceTier, Symbol, UniverseEntry

logger = logging.getLogger(__name__)

DEFAULT_MAX_WATCHLIST = 50
DEFAULT_MIN_ATR_PCT = 0.02
"""ATR% の下限。**往復コストの5倍**という根拠から決まる。

Stage A のスリッページは片道20bps = 往復40bps。日中値幅がその5倍ないと
コスト負けする。上限とは根拠がまったく別なので、片方を動かしても他方は動かない。
"""
DEFAULT_ATR_PERIOD = 14
DEFAULT_VOLUME_LOOKBACK_DAYS = 20
DEFAULT_PREMIUM_SCORE_MULTIPLIER = 1.3
DEFAULT_PREMIUM_MAX_CONCURRENT = 1

STAGE_A_FIELDS = ("atr_pct", "prev_volume_ratio", "prev_range_pct", "prev_close_position")
STAGE_B_FIELDS = ("gap_pct", "premarket_volume_ratio")

DEFAULT_STAGE_A_WEIGHTS: dict[str, float] = {
    "atr_pct": 0.40,
    "prev_volume_ratio": 0.25,
    "prev_range_pct": 0.20,
    "prev_close_position": 0.15,
}
"""既定の重み。config/universe.yaml の ``layer2.stage_a_weights`` と一致させること。

**これは未検証の初期値。** Phase 3 のウォークフォワード検証で確定する
（in-sample で決めて out-of-sample で検証する。in-sample の成績は数えない）。
"""


@dataclass(frozen=True)
class StageAFeatures:
    """Stage A のスコア指標。**前日引け時点の情報のみで構成する。**

    寄り前気配は kabuステーションAPI が必要なため Stage A では使えない
    （docs/09-data-sources.md §5）。

    これは妥協ではない。「前日引け時点の情報だけで翌日の監視銘柄を決める」のは
    実運用でも一般的な方式で、寄り前気配は流動性が薄くノイズが多いという批判もある。
    """

    atr_pct: float
    """14日ATR ÷ 株価。日中値幅の期待値"""
    prev_volume_ratio: float
    """前日出来高 ÷ 20日平均出来高。前日に注目が集まったか"""
    prev_range_pct: float
    """(前日高値 − 前日安値) ÷ 前日終値。ボラティリティの持続性"""
    prev_close_position: float
    """(終値 − 安値) ÷ (高値 − 安値)。前日の需給の決着"""


@dataclass(frozen=True)
class StageBFeatures:
    """Stage B で追加される指標。寄り前気配が必要。

    Stage B ではこれを追加して**予測力を比較検証する**:

    - Stage A スコア単体
    - Stage A スコア + 寄り前気配

    **予測力を持たないなら追加しない。**
    プレミアム枠の寄与検証と同じ構造で判定する。
    """

    gap_pct: float
    """寄り前気配 vs 前日終値。初動のエネルギー"""
    premarket_volume_ratio: float
    """寄り前気配の板の厚み ÷ 20日平均出来高。当日の注目度の代理変数"""


@dataclass(frozen=True)
class SelectorConfig:
    """Layer 2 の設定。config/universe.yaml の ``layer2`` に対応する。"""

    max_watchlist: int = DEFAULT_MAX_WATCHLIST
    min_atr_pct: float = DEFAULT_MIN_ATR_PCT
    max_atr_pct: float = field(default_factory=max_atr_pct)
    """ATR% の上限。**日次ブレーカーからの導出値**（`risk.limits.max_atr_pct`）。

    下限（コスト）とは根拠が別で、こちらは
    「1敗で当日が終わる銘柄を選ばない」ための制約。
    50万円・上限25%・損切り1.5×ATR では 5.33%。
    """
    atr_period: int = DEFAULT_ATR_PERIOD
    volume_lookback_days: int = DEFAULT_VOLUME_LOOKBACK_DAYS
    weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_STAGE_A_WEIGHTS)
    )
    premium_enabled: bool = True
    premium_score_multiplier: float = DEFAULT_PREMIUM_SCORE_MULTIPLIER
    premium_max_concurrent: int = DEFAULT_PREMIUM_MAX_CONCURRENT

    def __post_init__(self) -> None:
        if self.max_watchlist < 1:
            raise ValueError("max_watchlist は1以上")
        if self.atr_period < 1 or self.volume_lookback_days < 1:
            raise ValueError("atr_period と volume_lookback_days は1以上")
        if self.min_atr_pct < 0:
            raise ValueError("min_atr_pct は0以上")
        if self.max_atr_pct <= self.min_atr_pct:
            raise ValueError(
                f"min_atr_pct({self.min_atr_pct}) < max_atr_pct({self.max_atr_pct}) "
                "である必要がある"
            )
        if self.premium_max_concurrent < 0:
            raise ValueError("premium_max_concurrent は0以上")
        validate_weights(self.weights)

    @property
    def min_bars(self) -> int:
        """指標の計算に必要な日足の本数。

        ATR は前日終値との比較に1本余分に要る（True Range の定義）。
        """
        return max(self.atr_period + 1, self.volume_lookback_days)


def validate_weights(weights: Mapping[str, float]) -> None:
    """重みの妥当性を検証する。

    **黙って通さない。** 指標名の綴り違いを許すと、その指標の寄与が
    静かに 0 になり、スコアが変わったことに誰も気づけない。

    Raises:
        ValueError: 未知の指標名がある / 合計が 1.0 から離れている /
            負の重みがある場合。
    """
    known = set(STAGE_A_FIELDS) | set(STAGE_B_FIELDS)
    unknown = sorted(set(weights) - known)
    if unknown:
        raise ValueError(f"未知の指標名: {unknown}（既知: {sorted(known)}）")
    if not weights:
        raise ValueError("weights が空")
    negative = sorted(k for k, v in weights.items() if v < 0)
    if negative:
        raise ValueError(f"重みが負: {negative}")
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"重みの合計が 1.0 でない: {total}")


# --------------------------------------------------------------------------
# 指標の計算
# --------------------------------------------------------------------------


def true_range(bar: Bar, prev_close: float) -> float:
    """True Range。

    単純な高値−安値ではなく**前日終値からのギャップを含める**。
    寄り天・寄り底で始まった日の値幅を取りこぼさないため。
    """
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def compute_atr(bars: Sequence[Bar], period: int = DEFAULT_ATR_PERIOD) -> float | None:
    """直近 ``period`` 日の ATR（True Range の単純平均）。

    Wilder の平滑化ではなく単純平均を使う。**過去に指数的な重みを残さないため**で、
    「直近14日の値幅の期待値」という用途に対して解釈が素直になる。
    採用するなら平滑化版と成績を比較してから切り替える。

    Returns:
        ATR。バーが ``period + 1`` 本に満たなければ ``None``。
        **足りないのに計算して返さない** — 短い期間の平均は値幅の推定として
        信用できず、上場直後の銘柄が誤って上位に来る。
    """
    if period < 1 or len(bars) < period + 1:
        return None
    ranges = [
        true_range(bars[i], bars[i - 1].close) for i in range(len(bars) - period, len(bars))
    ]
    return sum(ranges) / period


def compute_features(
    bars: Sequence[Bar],
    atr_period: int = DEFAULT_ATR_PERIOD,
    volume_lookback_days: int = DEFAULT_VOLUME_LOOKBACK_DAYS,
) -> StageAFeatures | None:
    """前日引け時点の日足から Stage A の4指標を計算する。

    Args:
        bars: 時刻の昇順。**末尾が前営業日でなければならない。**
            当日のバーを含めてはならない（`build_candidates` が入口で落とす）。

    Returns:
        指標。本数が足りない、または前日終値が 0 以下なら ``None``。
    """
    if not bars:
        return None
    need = max(atr_period + 1, volume_lookback_days)
    if len(bars) < need:
        return None

    prev = bars[-1]
    if prev.close <= 0:
        return None

    atr = compute_atr(bars, atr_period)
    if atr is None:
        return None

    recent_volumes = [b.volume for b in bars[-volume_lookback_days:]]
    avg_volume = sum(recent_volumes) / volume_lookback_days
    # 出来高ゼロが続く銘柄は Layer 1 の HALTED で落ちているはずだが、
    # ゼロ除算を戦略側に漏らさないためここでも防ぐ。
    volume_ratio = prev.volume / avg_volume if avg_volume > 0 else 0.0

    day_range = prev.high - prev.low
    # 値幅ゼロ（ストップ張り付き等）は「どちらとも言えない」= 0.5 とする。
    # 0 や 1 に倒すと方向のヒントを捏造することになる。
    close_position = (prev.close - prev.low) / day_range if day_range > 0 else 0.5

    return StageAFeatures(
        atr_pct=atr / prev.close,
        prev_volume_ratio=volume_ratio,
        prev_range_pct=day_range / prev.close,
        prev_close_position=close_position,
    )


# --------------------------------------------------------------------------
# スコアリング
# --------------------------------------------------------------------------


def rank_normalize(values: Mapping[str, float]) -> dict[str, float]:
    """値を横断的な順位に変換し 0.0〜1.0 に正規化する。

    **なぜ生の値をそのまま加重合計しないのか。**
    ATR%（0.02 前後）と出来高比（1.0 前後）ではスケールが2桁違う。
    生値を足すと重みの設定に関係なく大きいスケールの指標が支配する。
    順位に変換すれば指標間でスケールが揃い、重みが意図どおり効く。

    同値には平均順位を与える（並び順という恣意的な要素で差がつかないように）。

    Returns:
        銘柄コード → 0.0〜1.0。要素が1つのときは 0.5（順位差が定義できない）。
    """
    if not values:
        return {}
    if len(values) == 1:
        return {code: 0.5 for code in values}

    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    result: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + j) / 2  # 0-origin の平均順位
        normalized = avg_rank / (n - 1)
        for k in range(i, j + 1):
            result[ordered[k][0]] = normalized
        i = j + 1
    return result


def score_all(
    features_by_symbol: Mapping[str, StageAFeatures],
    weights: Mapping[str, float] | None = None,
    stage_b_by_symbol: Mapping[str, StageBFeatures] | None = None,
) -> dict[str, float]:
    """母集団を横断してスコアを計算する。

    **1銘柄だけを見てスコアは決まらない。** 順位変換は母集団全体を見て
    初めて定義できるため、銘柄単位ではなくこの一括関数を入口にしている
    （銘柄ごとの `score()` を用意すると、呼び出し側が母集団を渡し忘れて
    生値の加重合計に退化する経路ができる）。

    Args:
        features_by_symbol: 銘柄コード → Stage A の指標。
        weights: 指標名 → 重み。省略時は `DEFAULT_STAGE_A_WEIGHTS`。
        stage_b_by_symbol: Stage B の追加指標。``None`` なら Stage A のみ。
            **全銘柄ぶん揃っている必要がある** — 一部だけ寄り前気配がある状態で
            混ぜると、気配が取れた銘柄だけが構造的に有利になる。

    Returns:
        銘柄コード → スコア（0.0〜1.0）。

    Note:
        Stage B で指標を追加するときは、**Stage A で確定した重みを流用しない**。
        指標が変われば最適な重みも変わるため、
        ウォークフォワード検証をやり直すこと（docs/03-universe.md §4.4）。
    """
    w = dict(weights) if weights is not None else dict(DEFAULT_STAGE_A_WEIGHTS)
    validate_weights(w)
    if not features_by_symbol:
        return {}

    used_stage_b = set(w) & set(STAGE_B_FIELDS)
    if used_stage_b and stage_b_by_symbol is None:
        raise ValueError(
            f"重みが Stage B の指標 {sorted(used_stage_b)} を含むが、"
            "寄り前気配が渡されていない"
        )
    if stage_b_by_symbol is not None:
        missing = sorted(set(features_by_symbol) - set(stage_b_by_symbol))
        if missing:
            raise ValueError(
                f"寄り前気配が欠けている銘柄がある: {missing[:5]}（計{len(missing)}）。"
                "一部だけ気配があると、その銘柄が構造的に有利になる"
            )

    ranked: dict[str, dict[str, float]] = {}
    for name in w:
        if name in STAGE_A_FIELDS:
            raw = {
                code: float(getattr(f, name)) for code, f in features_by_symbol.items()
            }
        else:
            assert stage_b_by_symbol is not None  # 上のチェックで保証済み
            raw = {
                code: float(getattr(stage_b_by_symbol[code], name))
                for code in features_by_symbol
            }
        ranked[name] = rank_normalize(raw)

    return {
        code: sum(w[name] * ranked[name][code] for name in w)
        for code in features_by_symbol
    }


# --------------------------------------------------------------------------
# 選定
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """スコアリングの入力。Layer 1 の通過銘柄に指標を紐づけたもの。"""

    symbol: Symbol
    tier: PriceTier
    features: StageAFeatures
    stage_b: StageBFeatures | None = None


def build_candidates(
    trade_date: date,
    symbols: Sequence[Symbol],
    bars_by_symbol: Mapping[str, tuple[Bar, ...]],
    tiers: Mapping[str, PriceTier],
    config: SelectorConfig | None = None,
) -> tuple[Candidate, ...]:
    """指標を計算して選定の入力を作る。

    **ここがルックアヘッド防止の関門。** ``trade_date`` 当日以降のバーを
    入口で捨ててから指標を計算するので、`compute_features` から未来を
    参照する経路が存在しない。呼び出し側が渡すバーに当日ぶんが混ざっていても
    結果は変わらない。

    Args:
        trade_date: 売買する日。この日のバーは使わない。
        symbols: Layer 1 を通過した銘柄。
        bars_by_symbol: 銘柄コード → 日足（時刻の昇順）。
        tiers: 銘柄コード → 株価レンジ枠。Layer 1 の `ScreenResult.tier`。

    Returns:
        指標を計算できた銘柄ぶん。本数不足の銘柄は黙って落とさず
        DEBUG ログに残す（母集団が想定より減った理由を後から追えるように）。
    """
    cfg = config or SelectorConfig()
    candidates: list[Candidate] = []
    skipped = 0

    for symbol in symbols:
        tier = tiers.get(symbol.code)
        if tier is None:
            skipped += 1
            logger.debug("%s: 株価レンジ枠が不明のため除外", symbol.code)
            continue

        past = tuple(
            b for b in bars_by_symbol.get(symbol.code, ()) if b.timestamp.date() < trade_date
        )
        features = compute_features(past, cfg.atr_period, cfg.volume_lookback_days)
        if features is None:
            skipped += 1
            logger.debug(
                "%s: 指標を計算できない（日足 %d本 / 必要 %d本）",
                symbol.code,
                len(past),
                cfg.min_bars,
            )
            continue

        candidates.append(Candidate(symbol=symbol, tier=tier, features=features))

    if skipped:
        logger.info(
            "Layer 2 入力: %d銘柄（指標を計算できず除外 %d銘柄）",
            len(candidates),
            skipped,
        )
    return tuple(candidates)


def select(
    candidates: Sequence[Candidate],
    trade_date: date,
    config: SelectorConfig | None = None,
) -> tuple[UniverseEntry, ...]:
    """当日の監視銘柄を選定する（docs/03-universe.md §3）。

    手順:

    1. 母集団を横断してスコアを計算する
    2. **ATR% が範囲外の銘柄を落とす**
       （下限: 日中値幅が往復コストの5倍ないとコスト負けする /
       上限: 1敗で日次ブレーカーに達する銘柄を採らない）
       **上限も見る**。ATR% が高すぎる銘柄は、上限比率で建てると
       1敗で日次ブレーカー（-2%）に達し当日が終わる
    3. 通常枠からスコア上位を採用する
    4. プレミアム枠は条件を満たす場合のみ追加する:

       - スコアが**通常枠採用銘柄のスコア中央値 × premium_score_multiplier 以上**
       - 同時採用は ``premium_max_concurrent`` 銘柄まで

    5. 全体を ``max_watchlist`` 件に切り詰める

    プレミアム枠のハードルは**通常枠の中央値**を基準にするため、
    通常枠が1銘柄も採用されなければプレミアム枠も採用しない
    （基準が定義できない状態で例外的に高株価を採るのは保守的でない）。

    Note:
        ``premium_score_multiplier`` の 1.3 は**暫定値**。Phase 3 で
        「プレミアム枠あり/なし」の成績を比較し、寄与を検証してから確定する。
        寄与しないなら枠ごと削除する（docs/03-universe.md §2）。

    Returns:
        監視対象。スコアの降順、最大 ``max_watchlist`` 件。
    """
    cfg = config or SelectorConfig()
    if not candidates:
        return ()

    codes = [c.symbol.code for c in candidates]
    duplicated = sorted({c for c in codes if codes.count(c) > 1})
    if duplicated:
        raise ValueError(f"銘柄コードが重複している: {duplicated}")

    features = {c.symbol.code: c.features for c in candidates}
    # 寄り前気配は全銘柄ぶん揃っているときだけ使う。
    # 一部だけ混ぜると、気配が取れた銘柄が構造的に有利になる。
    stage_b: dict[str, StageBFeatures] | None = None
    if all(c.stage_b is not None for c in candidates):
        stage_b = {
            c.symbol.code: c.stage_b for c in candidates if c.stage_b is not None
        }
    scores = score_all(features, cfg.weights, stage_b_by_symbol=stage_b)

    # 2. ATR% のハードルはスコアの前ではなく後に適用する。
    #    順位変換の母集団を先に削ると、残った銘柄の順位が母集団の切り方で変わる。
    #
    #    **下限と上限は根拠が別**なので、落ちた件数も別々に数える。
    #    下限が効きすぎているのか上限が効きすぎているのかで打つ手が変わる。
    too_quiet = [c for c in candidates if c.features.atr_pct < cfg.min_atr_pct]
    too_wild = [c for c in candidates if c.features.atr_pct > cfg.max_atr_pct]
    eligible = [
        c
        for c in candidates
        if cfg.min_atr_pct <= c.features.atr_pct <= cfg.max_atr_pct
    ]
    if too_quiet:
        logger.info(
            "ATR%% < %.2f%%（コスト負け）で除外: %d銘柄",
            cfg.min_atr_pct * 100,
            len(too_quiet),
        )
    if too_wild:
        logger.info(
            "ATR%% > %.2f%%（1敗で日次ブレーカー到達）で除外: %d銘柄",
            cfg.max_atr_pct * 100,
            len(too_wild),
        )
    if not eligible:
        logger.warning(
            "%s: ATR%% が %.2f%%〜%.2f%% に収まる銘柄がない。監視銘柄なし",
            trade_date,
            cfg.min_atr_pct * 100,
            cfg.max_atr_pct * 100,
        )
        return ()

    def sort_key(c: Candidate) -> tuple[float, str]:
        # 同点は銘柄コード昇順。実行のたびに並びが変わると再現しない
        return (-scores[c.symbol.code], c.symbol.code)

    normal = sorted(
        (c for c in eligible if c.tier is PriceTier.NORMAL), key=sort_key
    )[: cfg.max_watchlist]

    adopted = list(normal)

    if cfg.premium_enabled and cfg.premium_max_concurrent > 0 and normal:
        hurdle = (
            median(scores[c.symbol.code] for c in normal) * cfg.premium_score_multiplier
        )
        premium = [
            c
            for c in sorted(
                (c for c in eligible if c.tier is PriceTier.PREMIUM), key=sort_key
            )
            if scores[c.symbol.code] >= hurdle
        ][: cfg.premium_max_concurrent]
        if premium:
            logger.info(
                "プレミアム枠を採用: %s（ハードル %.4f）",
                ", ".join(c.symbol.code for c in premium),
                hurdle,
            )
        adopted.extend(premium)

    adopted.sort(key=sort_key)
    adopted = adopted[: cfg.max_watchlist]

    return tuple(
        UniverseEntry(
            symbol=c.symbol,
            trade_date=trade_date,
            price_tier=c.tier,
            score=scores[c.symbol.code],
            atr_pct=c.features.atr_pct,
            prev_volume_ratio=c.features.prev_volume_ratio,
            prev_range_pct=c.features.prev_range_pct,
            prev_close_position=c.features.prev_close_position,
            gap_pct=c.stage_b.gap_pct if c.stage_b else None,
            premarket_volume_ratio=(
                c.stage_b.premarket_volume_ratio if c.stage_b else None
            ),
        )
        for c in adopted
    )
