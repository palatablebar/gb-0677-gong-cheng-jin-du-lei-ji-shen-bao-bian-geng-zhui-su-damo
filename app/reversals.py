"""冲回链裁决。

一笔负进度(冲回)必须落到三件事上：
1. 原申报(origin)：显式 reversal_of，或同清单更早周期仍有额度的正向申报，
   或该笔申报自身的前期已审累计(previous_qty)。
2. 原已签量(signed_qty)：原申报当期被采信的实施量，或前期已审累计。
3. 已冲回余量(remaining)：原已签量减去已签批次累计冲回，再减去本批已分配。

采信量按剩余额度截断：额度内部分进入正式结果，超出部分记为超额并从净进度排除，
不整笔退回。同一凭证在本批重复出现、或已在已签批次使用，一律不重复扣减。
无原单可落的冲回单独隔离，宁可不签也不混进正式结果。
"""
from decimal import Decimal
from app.values import number

_EMPTY = Decimal(0)


def ledger(ledger_source):
    """归一化冲回台账。

    ledger_source 为 {"origins":…,"vouchers":…}，origins 支持
    claim_id -> 已冲回量(数字)，或 claim_id -> {"signed_qty","reversed_qty"}。
    vouchers 为已签批次用过的冲回凭证编号。
    """
    ledger_source = ledger_source or {}
    origins = {}
    for key, value in (ledger_source.get("origins") or {}).items():
        if isinstance(value, dict):
            signed, reversed_qty = value.get("signed_qty"), value.get("reversed_qty")
        else:
            signed, reversed_qty = None, value
        origins[str(key)] = {
            "signed": None if signed in (None, "") else number(signed),
            "reversed": _EMPTY if reversed_qty in (None, "") else number(reversed_qty),
        }
    vouchers = {str(v) for v in (ledger_source.get("vouchers") or [])}
    return origins, vouchers


def from_parameters(parameters):
    """显式参数中的台账与已签批次台账二选一，显式优先。"""
    return (parameters or {}).get("reversal_ledger")


def _text(value):
    return str(value or "").strip()


class _Origin:
    __slots__ = ("claim_id", "signed", "used", "period", "item")

    def __init__(self, claim_id, signed, used, period=None, item=None):
        self.claim_id, self.signed, self.used = claim_id, signed, used
        self.period, self.item = period, item

    @property
    def remaining(self):
        return self.signed - self.used


def _explicit(row, origins, sealed):
    explicit = _text(row.get("reversal_of"))
    if not explicit:
        return None
    if explicit in origins:
        return origins[explicit], "explicit"
    prior = sealed.get(explicit)
    if prior and prior["signed"] is not None:
        origin = _Origin(explicit, prior["signed"], prior["reversed"])
        origins[explicit] = origin
        return origin, "explicit_sealed"
    return False


def _infer(row, origins):
    candidates = [
        origin for origin in origins.values()
        if origin.item == row.get("item") and origin.period and row.get("period")
        and origin.period < row["period"]
    ]
    if not candidates:
        return None
    open_origins = [origin for origin in candidates if origin.remaining > 0]
    pool = open_origins or candidates
    return max(pool, key=lambda o: (o.period, o.claim_id)), "inferred_latest"


def _self(row, origins, sealed):
    key = _text(row.get("claim_id"))
    if key in origins:
        return origins[key], "self_previous"
    prior = sealed.get(key, {})
    signed = prior.get("signed")
    if signed is None:
        signed = number(row["previous_qty"])
    origin = _Origin(key, signed, prior.get("reversed", _EMPTY), row.get("period"), row.get("item"))
    origins[key] = origin
    return origin, "self_previous"


def build(rows, context):
    """裁决整批冲回链，返回 (accepted_rows, quarantined_rows, chain, metrics)。

    rows 为已通过前置校验的申报行，含正进度与反转=approved 的负进度。
    context["parameters"]["reversal_ledger"] 优先，其次 context["_reversal_ledger"]
    （已签批次台账，由调用方注入，不进入批次摘要）。
    """
    parameters = context.get("parameters") or {}
    sealed, sealed_vouchers = ledger(from_parameters(parameters) or context.get("_reversal_ledger"))
    origins, good, bad, chain = {}, [], [], []
    seen_vouchers = set()

    # 先登记原申报：只有正进度才构成可冲回的原已签量。
    for row in rows:
        if number(row["increment_qty"]) > 0:
            claim_id = row["claim_id"]
            origins[claim_id] = _Origin(
                claim_id, number(row["increment_qty"]),
                sealed.get(claim_id, {}).get("reversed", _EMPTY),
                row.get("period"), row.get("item"),
            )

    for row in rows:
        increment = number(row["increment_qty"])
        if increment >= 0:
            good.append(row)
            continue

        voucher = _text(row.get("voucher_id")) or row["claim_id"]
        if voucher in seen_vouchers:
            bad.append({**row, "reason": "DUPLICATE_VOUCHER",
                        "detail": f"冲回凭证 {voucher} 在本批重复出现，未再次扣减"})
            continue
        seen_vouchers.add(voucher)
        if voucher in sealed_vouchers:
            bad.append({**row, "reason": "REPLAY_VOUCHER",
                        "detail": f"冲回凭证 {voucher} 已在已签批次使用，未重复扣减"})
            continue

        resolved = _explicit(row, origins, sealed)
        if resolved is False:
            bad.append({**row, "reason": "ORIGIN_NOT_FOUND",
                        "detail": f"冲回指向的原申报 {_text(row.get('reversal_of'))} 不存在"})
            continue
        origin, basis = resolved or _infer(row, origins) or _self(row, origins, sealed)

        requested = -increment
        remaining = origin.remaining
        if remaining <= 0:
            bad.append({**row, "reason": "EXCESS_REVERSAL",
                        "detail": f"原申报 {origin.claim_id} 已无剩余可冲回量，凭证 {voucher} 超额"})
            chain.append(_entry(row, voucher, origin, basis, requested, _EMPTY, requested, basis))
            continue

        accepted = min(requested, remaining)
        excess = requested - accepted
        prior = origin.used
        origin.used += accepted
        good.append({**row,
                     "increment_qty": str(-accepted),
                     "gross_amount": str(-accepted * number(row["unit_price"])),
                     "reversal_voucher": voucher,
                     "reversal_origin": origin.claim_id,
                     "reversal_basis": basis,
                     "reversal_origin_signed": str(origin.signed),
                     "reversal_prior": str(prior),
                     "reversal_accepted": str(accepted),
                     "reversal_excess": str(excess),
                     "reversal_remaining": str(origin.remaining),
                     "reversal_status": "部分采信" if excess > 0 else "全额采信"})
        chain.append(_entry(row, voucher, origin, basis, requested, accepted, excess, prior))

    metrics = {
        "reversals": len(chain),
        "reversal_accepted_qty": str(sum((number(e["accepted_qty"]) for e in chain), _EMPTY)),
        "reversal_excess_qty": str(sum((number(e["excess_qty"]) for e in chain), _EMPTY)),
        "reversal_rejected": sum(1 for r in bad if r["reason"] in {
            "EXCESS_REVERSAL", "REPLAY_VOUCHER", "DUPLICATE_VOUCHER", "ORIGIN_NOT_FOUND"}),
        "reversal_chain": chain,
    }
    return good, bad, chain, metrics


def _entry(row, voucher, origin, basis, requested, accepted, excess, prior):
    return {
        "voucher_id": voucher,
        "claim_id": row.get("claim_id", ""),
        "item": row.get("item", ""),
        "period": row.get("period", ""),
        "origin_claim_id": origin.claim_id,
        "origin_period": origin.period or "",
        "origin_basis": basis,
        "origin_signed_qty": str(origin.signed),
        "already_reversed_qty": str(prior),
        "requested_qty": str(requested),
        "accepted_qty": str(accepted),
        "excess_qty": str(excess),
        "remaining_qty": str(origin.signed - origin.used),
        "source_line": row.get("_line", ""),
    }
