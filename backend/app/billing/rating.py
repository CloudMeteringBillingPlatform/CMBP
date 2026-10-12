"""Pricing usage with a plan version (spec section 9). Money is always integer paise.

Each metric's rate card is a list of tiers over the period's total quantity. A flat metric is
two tiers: its included allowance at rate 0, then one rate. api.requests is genuinely tiered.

`charge(card, start, end)` prices the units numbered [start, end) of a period's total. Rating a
whole period is charge(card, 0, total). A late adjustment is priced at the margin of what was
already billed: charge(card, billed, billed + late) — the "marginal tier of the closed period's
final total" rule from the open decisions.
"""

from dataclasses import dataclass
from fractions import Fraction

METRIC_LABELS = {
    "api.requests": "API requests",
    "storage.bytes_in": "Storage ingress",
    "storage.bytes_out": "Storage egress",
    "storage.byte_hours": "Storage held",
    "image.bytes_processed": "Image bytes processed",
    "image.cpu_seconds": "Image CPU time",
}


@dataclass
class Segment:
    metric: str
    description: str
    quantity: int
    unit_label: str
    unit_size: int
    rate_paise: int
    amount: Fraction  # exact; rounded once per line


def round_paise(amount: Fraction) -> int:
    """Round half up to whole paise."""
    return int((amount.numerator * 2 + amount.denominator) // (amount.denominator * 2))


def tiers(card: dict) -> list[tuple[int, int | None, int]]:
    """Normalize a rate card into [(lower, upper_or_None, rate_paise_per_unit_size)]."""
    included = int(card.get("included", 0))
    result: list[tuple[int, int | None, int]] = []
    lower = 0
    if included > 0:
        result.append((0, included, 0))
        lower = included
    if "tiers" in card:
        for tier in card["tiers"]:
            upper = tier.get("up_to")
            upper = None if upper is None else int(upper)
            if upper is not None and upper <= lower:
                continue
            result.append((lower, upper, int(tier["rate"])))
            if upper is None:
                break
            lower = upper
    else:
        result.append((lower, None, int(card["rate"])))
    return result


def _fmt_qty(quantity: int, unit_size: int, unit_label: str) -> str:
    value = quantity / unit_size
    text = f"{value:,.3f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
    return f"{text} {unit_label}"


def _fmt_rate(rate: int, unit_label: str) -> str:
    return f"₹{rate / 100:,.2f} / {unit_label}"


def charge(metric: str, card: dict, start: int, end: int) -> list[Segment]:
    if end <= start:
        return []
    unit_size = int(card["unit_size"])
    unit_label = str(card["unit_label"])
    label = METRIC_LABELS.get(metric, metric)
    segments = []
    for lower, upper, rate in tiers(card):
        lo = max(start, lower)
        hi = end if upper is None else min(end, upper)
        if hi <= lo:
            continue
        qty = hi - lo
        if rate == 0:
            what = "included" if lower == 0 else "free tier"
        else:
            what = _fmt_rate(rate, unit_label)
        bounds = f"{lower:,}–{upper:,}" if upper is not None else f"over {lower:,}"
        description = f"{label}: {_fmt_qty(qty, unit_size, unit_label)} ({what}; tier {bounds})"
        segments.append(
            Segment(
                metric=metric,
                description=description,
                quantity=qty,
                unit_label=unit_label,
                unit_size=unit_size,
                rate_paise=rate,
                amount=Fraction(qty * rate, unit_size),
            )
        )
    return segments


def rate_totals(rates: dict, totals: dict[str, int]) -> list[Segment]:
    segments: list[Segment] = []
    for metric in METRIC_LABELS:
        quantity = int(totals.get(metric, 0))
        card = rates.get(metric)
        if card and quantity > 0:
            segments.extend(charge(metric, card, 0, quantity))
    return segments


def cost_paise(rates: dict, totals: dict[str, int]) -> int:
    return sum(round_paise(s.amount) for s in rate_totals(rates, totals))
