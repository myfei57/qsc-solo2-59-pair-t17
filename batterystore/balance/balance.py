"""均衡组件。

``Balancer`` 把目标选择、脉冲节拍与状态机串成一条链：启动时选出需要放电的
单体，之后每个控制周期推进一个节拍并把被选单体的电压往簇内最低值拉。

完成判据只有一条：压差回到目标带内。脉冲轮次用尽是兜底而不是第二种完成——
轮次用尽压差仍越限说明这簇不收敛，状态置 ``aborted``，充电照样拒绝，必须
人工复位后重跑，绝不允许"跑够次数就算过"。

充电放行通过 :meth:`gate` 取结果：只认"当前额定代际下的 complete"。均衡被
中止（超时 / 轮次用尽 / 换芯作废）时给出的是中止档及原因，同时带当前压差、
距目标还差多少毫伏、还剩多少轮次，拒绝理由可直接进审计。
"""

from __future__ import annotations

from ..config import BalanceLimits
from ..rack.cells import Cell
from .run import BalanceRun
from .select import cell_delta_mv, select_cells
from .snapshot import BalanceSnapshot
from .state import BalanceState


class Balancer:
    """电池均衡组件。"""

    def __init__(self, limits: BalanceLimits) -> None:
        self._limits = limits
        self._state = BalanceState()
        self._run = BalanceRun(limits)
        self._delta_mv = 0.0
        self._rating_epoch: int | None = None

    @property
    def limits(self) -> BalanceLimits:
        return self._limits

    @property
    def state(self) -> BalanceState:
        return self._state

    @property
    def rating_epoch(self) -> int | None:
        """返回当前判据绑定的额定值代际。"""

        return self._rating_epoch

    def is_complete(self) -> bool:
        """均衡是否已完成（不校验额定代际，充电放行请用 :meth:`gate`）。"""

        return self._state.is_complete()

    def delta_mv(self) -> float:
        """返回最近一次测得的压差。"""

        return self._delta_mv

    def remaining_mv(self) -> float:
        """压差距目标带还差多少毫伏（已在带内为 0）。"""

        return max(0.0, self._delta_mv - self._limits.target_delta_mv)

    def start(self, cells: list[Cell], rating_epoch: int = 0) -> dict:
        """启动一次均衡，返回选定目标。

        判据绑定到 ``rating_epoch``：之后若换了电芯、代际前进，这次的结论自动
        作废，不能拿旧代际的"完成"给新电芯放行。
        """

        self._rating_epoch = int(rating_epoch)
        targets = select_cells(cells, self._limits.target_delta_mv, self._limits.min_cells)
        self._delta_mv = cell_delta_mv(cells)
        if not targets:
            self._run.begin([])
            self._state.transition("complete", "delta_within_target")
            return {"targets": [], "delta_mv": round(self._delta_mv, 2), "started": False}
        self._run.begin(targets)
        self._state.transition("running", "targets_selected")
        return {"targets": targets, "delta_mv": round(self._delta_mv, 2), "started": True}

    def advance(self, dt: float, cells: list[Cell]) -> dict:
        """推进一个均衡节拍，返回本拍结果。"""

        if self._state.state != "running":
            return {
                "phase": "idle",
                "delta_mv": round(self._delta_mv, 2),
                "complete": self.is_complete(),
            }
        phase = self._run.advance(dt)
        if phase == "pulse":
            self._apply_pulse(cells)
        self._delta_mv = cell_delta_mv(cells)
        if self._delta_mv <= self._limits.target_delta_mv:
            self._state.transition("complete", "delta_within_target")
        elif self._run.pulses_exhausted():
            # 兜底：轮次用尽仍不收敛，判中止而不是完成，充电不得放行。
            self._state.transition("aborted", "pulses_exhausted")
        elif self._run.overrun():
            self._state.transition("aborted", "duration_exceeded")
        return {
            "phase": phase,
            "delta_mv": round(self._delta_mv, 2),
            "remaining_mv": round(self.remaining_mv(), 2),
            "complete": self._state.is_complete(),
            "run": self._run.summary(),
        }

    def invalidate(self, current_rating_epoch: int, reason: str = "rating_changed") -> dict:
        """额定值代际变化时作废旧判据，返回作废前的档位。

        正在运行或已完成的结论都按中止处理（原因 ``rating_invalidated``），并且
        故意保留旧代际绑定：这样在按新额定值重新 :meth:`start` 之前，充电门拿到
        的永远是"判据代际落后"，明确拒绝而不会被误读成普通中止；本就空闲或已
        中止的档位只记一笔作废、不重复计数。
        """

        previous = self._state.state
        self._run.begin([])
        if previous in ("running", "complete"):
            self._state.transition("aborted", "rating_invalidated")
        return {
            "previous": previous,
            "reason": reason,
            "bound_rating_epoch": self._rating_epoch,
            "current_rating_epoch": int(current_rating_epoch),
        }

    def reset(self) -> dict:
        """复位均衡状态，返回复位前的档位。"""

        previous = self._state.state
        self._run.begin([])
        self._state.transition("idle", "reset")
        return {"previous": previous, "counts": self._state.counts()}

    def gate(self, current_rating_epoch: int | None = None) -> dict:
        """返回充电联锁核对结果。

        ``passed`` 仅当状态为 complete 且判据代际与当前额定值一致。其余情况
        一律拒绝，并说清当前压差、距目标差值、剩余轮次以及被拒原因。
        """

        state = self._state.state
        reason = self._state.block_reason()
        # 已经得出过结论（完成/中止）或正在跑的判据才绑定额定代际；从未启动
        # （idle）时本来就不通过，按"未完成"拒绝，不混成换芯作废。
        if state != "idle" and self._rating_epoch != current_rating_epoch:
            reason = "balance_rating_invalidated"
        passed = state == "complete" and reason is None
        return {
            "passed": passed,
            "state": state,
            "reason": reason,
            "delta_mv": round(self._delta_mv, 2),
            "target_delta_mv": round(self._limits.target_delta_mv, 2),
            "remaining_mv": round(self.remaining_mv(), 2),
            "abort_reason": self._state.reason_for("aborted") if state == "aborted" else None,
            "pulses": self._run.pulses(),
            "max_pulses": self._limits.max_pulses,
            "pulses_remaining": self._run.pulses_remaining(),
            "rating_epoch": self._rating_epoch,
            "current_rating_epoch": current_rating_epoch,
        }

    def _apply_pulse(self, cells: list[Cell]) -> None:
        """给被选中的单体放电一个步长。"""

        targets = set(self._run.targets())
        if not targets:
            return
        floor = min(cell.voltage_v for cell in cells)
        for cell in cells:
            if cell.cell_id in targets and cell.voltage_v > floor:
                cell.update(voltage_v=cell.voltage_v - min(0.002, (cell.voltage_v - floor) / 2.0))

    def snapshot(self) -> BalanceSnapshot:
        """返回均衡状态快照。"""

        return BalanceSnapshot(
            state=self._state.state,
            state_reason=self._state.reason_for(self._state.state),
            counts=self._state.counts(),
            run=self._run.summary(),
            targets=self._run.targets(),
            delta_mv=self._delta_mv,
            remaining_mv=self.remaining_mv(),
            target_delta_mv=self._limits.target_delta_mv,
            rating_epoch=self._rating_epoch,
        )
