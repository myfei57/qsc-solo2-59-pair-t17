"""充电计划。

充电放行是三件事的合取：均衡联锁通过、温控降额系数已按最新额定值算好、SOC
还有可充空间。均衡这一档不再只传一个布尔值，而是吃 :class:`Balancer.gate`
的完整结论——没跑完是 ``balance_incomplete``，被中止（超时 / 轮次用尽 /
换芯作废）是 ``balance_aborted`` 或 ``balance_rating_invalidated``，任何一档
都不会被当成通过；拒绝时同时带上当前压差与距目标差值，审计里能看到"现在
压差多少、还差多少"，而不是一句"充电失败"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import BmsLimits, RackLimits
from ..rack.rack import Rack
from .voltage import charge_headroom_mv, current_for_headroom


@dataclass(frozen=True)
class ChargePlan:
    """一次充电的计划与前提条件核对结果。"""

    rack_id: str
    allowed: bool
    current_a: float
    headroom_mv: float
    derate_ratio: float
    reasons: list[str] = field(default_factory=list)
    balance: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "allowed": self.allowed,
            "current_a": round(self.current_a, 3),
            "headroom_mv": round(self.headroom_mv, 2),
            "derate_ratio": round(self.derate_ratio, 4),
            "reasons": list(self.reasons),
            "balance": dict(self.balance),
        }


def build_charge_plan(
    rack: Rack,
    pcs_limits,
    limits: BmsLimits,
    rack_limits: RackLimits,
    *,
    balance_gate: dict,
    temp_derate_ratio: float,
    delta_mv: float,
) -> ChargePlan:
    """按均衡联锁、降额与电压裕度构造充电计划。"""

    reasons: list[str] = []
    if not balance_gate.get("passed"):
        reasons.append(balance_gate.get("reason") or "balance_incomplete")
    if temp_derate_ratio <= 0.0:
        reasons.append("derate_blocked")
    if not limits.soc_window_ok(rack.soc_pct()):
        reasons.append("soc_out_of_window")
    if delta_mv > balance_gate.get("target_delta_mv", limits.ready_delta_mv):
        reasons.append("cell_delta_high")

    headroom = charge_headroom_mv(rack, rack_limits)
    allowed_current = current_for_headroom(headroom, pcs_limits) * max(0.0, temp_derate_ratio)
    if allowed_current <= 0.0:
        reasons.append("voltage_headroom_exhausted")
    return ChargePlan(
        rack_id=rack.rack_id,
        allowed=not reasons,
        current_a=allowed_current,
        headroom_mv=headroom,
        derate_ratio=temp_derate_ratio,
        reasons=reasons,
        balance={
            "state": balance_gate.get("state"),
            "delta_mv": balance_gate.get("delta_mv"),
            "remaining_mv": balance_gate.get("remaining_mv"),
            "target_delta_mv": balance_gate.get("target_delta_mv"),
            "abort_reason": balance_gate.get("abort_reason"),
            "pulses_remaining": balance_gate.get("pulses_remaining"),
        },
    )
