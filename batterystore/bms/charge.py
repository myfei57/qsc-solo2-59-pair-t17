"""充电计划。

充电放行是三件事的合取：均衡放行门通过、温控降额系数已按最新额定值算好、
SOC 还有可充空间。均衡这一条不再只看一个布尔位，而是看
:meth:`Balancer.gate` 的完整结论——状态档、簇、额定值代际、单体指纹都对得上
才算通过；中止、未运行、换芯后结论过期都会被拒。拒绝原因连同"现在压差
多少、距目标带还差多少"一起进计划，审计里能看到具体卡在哪，而不是一句
"充电失败"。
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
    balance_gate: dict | None = None

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "allowed": self.allowed,
            "current_a": round(self.current_a, 3),
            "headroom_mv": round(self.headroom_mv, 2),
            "derate_ratio": round(self.derate_ratio, 4),
            "reasons": list(self.reasons),
            "balance_gate": dict(self.balance_gate) if self.balance_gate else None,
        }


def build_charge_plan(
    rack: Rack,
    pcs_limits,
    limits: BmsLimits,
    rack_limits: RackLimits,
    *,
    balance_complete: bool,
    temp_derate_ratio: float,
    balance_min_delta_mv: float,
    delta_mv: float,
    balance_gate: dict | None = None,
) -> ChargePlan:
    """按均衡、降额与电压裕度构造充电计划。

    优先使用 ``balance_gate``（:meth:`Balancer.gate` 的结论）；没给门结论时
    退回旧的布尔位核对，保留直接构造计划的调用路径可用。
    """

    reasons: list[str] = []
    gate_passed = bool(balance_gate.get("passed")) if balance_gate is not None else None
    if balance_gate is not None:
        if not gate_passed:
            reasons.append(balance_gate.get("reason") or "balance_incomplete")
        balance_complete = gate_passed
    elif not balance_complete:
        reasons.append("balance_incomplete")
    if temp_derate_ratio <= 0.0:
        reasons.append("derate_blocked")
    if not limits.soc_window_ok(rack.soc_pct()):
        reasons.append("soc_out_of_window")
    # 均衡已按判据完成（含轮次兜底完成）时，不再用裸压差二次拦截，避免
    # "判据说完成、计划又说越限"互相打架；门已给出精确拒绝原因（running/
    # aborted/stale…）时同样不再叠加裸压差重复原因；只有走旧布尔位路径
    # 且未完成时才保留压差越限提示。
    if gate_passed is None and not balance_complete and delta_mv > balance_min_delta_mv:
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
        balance_gate=balance_gate,
    )
