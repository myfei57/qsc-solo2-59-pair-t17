"""均衡状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BalanceSnapshot:
    """均衡某一时刻的状态。"""

    state: str
    counts: dict
    run: dict
    targets: list
    delta_mv: float
    target_delta_mv: float
    state_reason: str = ""
    remaining_mv: float = 0.0
    rating_epoch: int | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "state_reason": self.state_reason,
            "counts": dict(self.counts),
            "run": dict(self.run),
            "targets": list(self.targets),
            "delta_mv": round(self.delta_mv, 2),
            "remaining_mv": round(self.remaining_mv, 2),
            "target_delta_mv": round(self.target_delta_mv, 2),
            "rating_epoch": self.rating_epoch,
        }
