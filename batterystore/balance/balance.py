"""均衡组件。

``Balancer`` 把目标选择、脉冲节拍与状态机串成一条链：启动时选出需要放电的
单体，之后每个控制周期推进一个节拍并把被选单体的电压往簇内最低值拉。

完成判据按配置二选一：

* ``delta``（默认，更准）：压差回到目标带即完成；脉冲轮次用尽仍未收敛判
  中止，防止不收敛的簇一直跑；
* ``pulses``（兜底）：脉冲轮次用尽即判据结束；用尽时压差已在目标带内才算
  完成，压差仍越带说明没补平，判中止，充电不得放行。

两种策略共用一条硬中止线：运行时长超限一律 ``aborted``。

充电放行只认 ``gate()`` 返回的通过结论，而不是简单看状态档：结论与簇
（``rack_id``）、额定值代际（``rating_epoch``）以及单体指纹绑定，均衡期间
一旦换电芯或单体组成变化，旧结论立即作废，必须按新额定值重跑。
"""

from __future__ import annotations

import hashlib

from ..config import BalanceLimits
from ..rack.cells import Cell
from .run import BalanceRun
from .select import cell_delta_mv, select_cells
from .snapshot import BalanceSnapshot
from .state import BalanceState


def cells_fingerprint(cells: list[Cell]) -> str:
    """计算单体组成与当前额定值的指纹。

    换电芯会改额定值、增减单体会改组成，指纹都随之变化；仅电压采集波动不改
    指纹，正常均衡放电不会误触发作废。
    """

    digest = hashlib.sha1()
    for cell in cells:
        digest.update(cell.cell_id.encode("utf-8"))
        digest.update(f"{cell.rating_v:.4f}".encode("utf-8"))
    return digest.hexdigest()[:16]


class Balancer:
    """电池均衡组件。"""

    def __init__(self, limits: BalanceLimits) -> None:
        self._limits = limits
        self._state = BalanceState()
        self._run = BalanceRun(limits)
        self._delta_mv = 0.0
        self._rack_id: str | None = None
        self._rating_epoch: int | None = None
        self._fingerprint: str | None = None
        self._invalidated_reason: str | None = None

    @property
    def limits(self) -> BalanceLimits:
        return self._limits

    @property
    def state(self) -> BalanceState:
        return self._state

    def is_complete(self) -> bool:
        """均衡是否已完成（不校验簇是否换过；放行核对请用 :meth:`gate`）。"""

        return self._state.is_complete()

    def delta_mv(self) -> float:
        """返回最近一次测得的压差。"""

        return self._delta_mv

    def remaining_mv(self) -> float:
        """返回距目标带还差多少毫伏（已在带内为零）。"""

        return max(0.0, self._delta_mv - self._limits.target_delta_mv)

    def start(
        self,
        cells: list[Cell],
        *,
        rack_id: str | None = None,
        rating_epoch: int = 0,
    ) -> dict:
        """启动一次均衡，返回选定目标。

        同时绑定簇标识、额定值代际与单体指纹，供之后的放行核对确认这期间
        没有换电芯。
        """

        self._rack_id = rack_id
        self._rating_epoch = int(rating_epoch)
        self._fingerprint = cells_fingerprint(cells)
        self._invalidated_reason = None
        targets = select_cells(cells, self._limits.target_delta_mv, self._limits.min_cells)
        self._delta_mv = cell_delta_mv(cells)
        if not targets:
            # 快通道：启动前压差已在目标带内，直接判完成，不消耗脉冲轮次。
            self._run.begin([])
            self._state.transition("complete", "delta_within_target")
            return {
                "targets": [],
                "delta_mv": round(self._delta_mv, 2),
                "remaining_mv": 0.0,
                "started": False,
            }
        self._run.begin(targets)
        self._state.transition("running", "targets_selected")
        return {
            "targets": targets,
            "delta_mv": round(self._delta_mv, 2),
            "remaining_mv": round(self.remaining_mv(), 2),
            "started": True,
        }

    def advance(self, dt: float, cells: list[Cell]) -> dict:
        """推进一个均衡节拍，按所选判据收尾，返回本拍结果。"""

        if self._state.state != "running":
            return {
                "phase": "idle",
                "delta_mv": round(self._delta_mv, 2),
                "remaining_mv": round(self.remaining_mv(), 2),
                "complete": self.is_complete(),
            }
        if self._fingerprint is not None and cells_fingerprint(cells) != self._fingerprint:
            # 节拍中发现单体组成或额定值变化：旧判据立即作废。
            self.invalidate("cells_changed_mid_run")
            return {
                "phase": "idle",
                "delta_mv": round(cell_delta_mv(cells), 2),
                "remaining_mv": round(self.remaining_mv(), 2),
                "complete": False,
                "invalidated": True,
            }
        phase = self._run.advance(dt)
        if phase == "pulse":
            self._apply_pulse(cells)
        self._delta_mv = cell_delta_mv(cells)
        within_target = self._delta_mv <= self._limits.target_delta_mv
        finished = self._finish(within_target)
        return {
            "phase": phase,
            "delta_mv": round(self._delta_mv, 2),
            "remaining_mv": round(self.remaining_mv(), 2),
            "complete": self.is_complete(),
            "within_target": within_target,
            "finish_reason": finished,
            "run": self._run.summary(),
        }

    def _finish(self, within_target: bool) -> str | None:
        """按判据与硬限制决定本拍如何收尾，返回收尾原因；没收尾返回 None。"""

        # 时长硬中止优先于一切：任何策略下超时都是 aborted，绝不放行。
        if self._run.overrun():
            self._state.transition("aborted", "duration_exceeded")
            return "duration_exceeded"
        if within_target:
            # 压差策略下这是主判据；轮次策略下提前收敛同样算完成，
            # 不必非把脉冲烧完。
            self._state.transition("complete", "delta_within_target")
            return "delta_within_target"
        if self._run.pulses_exhausted():
            if self._limits.completion_policy == "pulses":
                # 兜底判据：轮次用尽，但必须确认压差真的进了目标带。
                self._state.transition(
                    "aborted",
                    "pulse_budget_exhausted_delta_high",
                )
                return "pulse_budget_exhausted_delta_high"
            self._state.transition("aborted", "pulse_budget_exceeded_non_converging")
            return "pulse_budget_exceeded_non_converging"
        return None

    def abort(self, reason: str = "manual_abort") -> dict:
        """显式中止均衡（人工停止）。只在运行中生效，中止不可被充电放行。"""

        previous = self._state.state
        if previous == "running":
            self._state.transition("aborted", reason)
        return {"previous": previous, "state": self._state.state, "reason": self._state.terminal_reason()}

    def invalidate(self, reason: str = "rating_changed") -> dict:
        """作废当前判据并回到待启动态。

        均衡期间换电芯走这里：之前的"完成/中止"结论全部不算数，必须按新
        额定值重新启动一次均衡。作废不是中止、更不是完成。
        """

        previous = self._state.state
        self._run.begin([])
        self._rack_id = None
        self._rating_epoch = None
        self._fingerprint = None
        self._invalidated_reason = reason
        self._state.transition("idle", f"invalidated:{reason}")
        return {"previous": previous, "state": "idle", "reason": reason}

    def reset(self) -> dict:
        """复位均衡状态，返回复位前的档位。"""

        previous = self._state.state
        self._run.begin([])
        self._rack_id = None
        self._rating_epoch = None
        self._fingerprint = None
        self._invalidated_reason = None
        self._state.transition("idle", "reset")
        return {"previous": previous, "counts": self._state.counts()}

    def gate(self, rack_id: str, rating_epoch: int, cells: list[Cell]) -> dict:
        """充电放行门：唯一被充电联锁认可的均衡核对。

        只在以下条件同时成立时放行：状态为完成、结论属于这一簇、额定值代际
        一致、单体组成/额定值指纹一致。否则给出明确的拒绝原因，并附带当前
        压差、目标带与还差多少，调用方直接拿去展示或写审计。
        """

        delta_mv = cell_delta_mv(cells)
        remaining = max(0.0, delta_mv - self._limits.target_delta_mv)
        base = {
            "rack_id": rack_id,
            "state": self._state.state,
            "passed": False,
            "delta_mv": round(delta_mv, 2),
            "target_delta_mv": self._limits.target_delta_mv,
            "remaining_mv": round(remaining, 2),
            "pulses_left": self._run.pulses_left(),
            "max_pulses": self._limits.max_pulses,
            "completion_policy": self._limits.completion_policy,
            "terminal_reason": self._state.terminal_reason(),
        }
        if self._state.state == "running":
            return {**base, "reason": "balance_running"}
        if self._state.state == "aborted":
            return {**base, "reason": "balance_aborted"}
        if self._state.state == "idle":
            return {**base, "reason": "balance_not_run"}
        if self._rack_id != rack_id:
            return {**base, "reason": "balance_verdict_other_rack"}
        if self._rating_epoch != int(rating_epoch):
            # 均衡之后换过电芯：旧结论按旧额定值得出，必须作废重跑。
            return {**base, "reason": "balance_verdict_stale_rating_epoch"}
        if self._fingerprint is not None and cells_fingerprint(cells) != self._fingerprint:
            return {**base, "reason": "balance_verdict_stale_cells"}
        return {**base, "passed": True, "reason": None}

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
            terminal_reason=self._state.terminal_reason(),
            invalidated_reason=self._invalidated_reason,
            counts=self._state.counts(),
            run=self._run.summary(),
            targets=self._run.targets(),
            delta_mv=self._delta_mv,
            remaining_mv=self.remaining_mv(),
            target_delta_mv=self._limits.target_delta_mv,
            completion_policy=self._limits.completion_policy,
            rack_id=self._rack_id,
            rating_epoch=self._rating_epoch,
        )
