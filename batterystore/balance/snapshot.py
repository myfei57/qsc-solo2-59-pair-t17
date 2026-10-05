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
    remaining_mv: float = 0.0
    terminal_reason: str | None = None
    invalidated_reason: str | None = None
    completion_policy: str = "delta"
    rack_id: str | None = None
    rating_epoch: int | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "terminal_reason": self.terminal_reason,
            "invalidated_reason": self.invalidated_reason,
            "counts": dict(self.counts),
            "run": dict(self.run),
            "targets": list(self.targets),
            "delta_mv": round(self.delta_mv, 2),
            "remaining_mv": round(self.remaining_mv, 2),
            "target_delta_mv": round(self.target_delta_mv, 2),
            "completion_policy": self.completion_policy,
            "rack_id": self.rack_id,
            "rating_epoch": self.rating_epoch,
        }
