"""均衡状态机。

均衡只有四档：``idle``、``running``、``complete``、``aborted``。充电放行只看
``complete``，因此均衡被中止或还没跑完时，充电请求会被联锁拒绝，不会出现
"低电压电芯没补平就开充"。中止还要区分原因（超时 / 脉冲轮次用尽 / 换芯
作废），状态机记住每一档最后一次迁入的原因，充电被拒时审计里能直接看到
"为什么没通过、还差多少"，而不是把中止混成完成。
"""

from __future__ import annotations

import threading

STATES = ("idle", "running", "complete", "aborted")

# 非完成档 → 充电联锁拒绝原因。
BLOCK_REASONS = {
    "idle": "balance_incomplete",
    "running": "balance_incomplete",
    "aborted": "balance_aborted",
}


class BalanceState:
    """带计数的均衡状态机。"""

    def __init__(self) -> None:
        self._state = "idle"
        self._runs = 0
        self._completions = 0
        self._aborts = 0
        self._last_reason: dict[str, str] = {"idle": "init"}
        self._history: list[dict] = []
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def transition(self, target: str, reason: str) -> str:
        """切换状态并记录迁移。"""

        if target not in STATES:
            raise ValueError(f"未知的均衡状态: {target}")
        with self._lock:
            previous = self._state
            self._state = target
            if target == "running":
                self._runs += 1
            elif target == "complete":
                self._completions += 1
            elif target == "aborted":
                self._aborts += 1
            self._last_reason[target] = reason
            self._history.append({"from": previous, "to": target, "reason": reason})
            return self._state

    def reason_for(self, state: str) -> str:
        """返回某一档最后一次迁入的原因。"""

        with self._lock:
            return self._last_reason.get(state, "never_entered")

    def is_complete(self) -> bool:
        """均衡是否已完成。"""

        return self.state == "complete"

    def block_reason(self) -> str | None:
        """返回当前档对充电的拒绝原因；完成时返回 ``None``。"""

        return BLOCK_REASONS.get(self.state)

    def counts(self) -> dict:
        """返回运行、完成与中止计数。"""

        with self._lock:
            return {"runs": self._runs, "completions": self._completions, "aborts": self._aborts}

    def history(self, limit: int = 20) -> list[dict]:
        """返回最近的迁移记录。"""

        with self._lock:
            return [dict(item) for item in self._history[-limit:]]
