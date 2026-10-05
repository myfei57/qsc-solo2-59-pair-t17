"""均衡状态机。

均衡有四档：``idle``、``running``、``complete``、``aborted``。充电放行只看
``complete`` 这一档，因此：

* 均衡还在跑（``running``）——充电被联锁拒绝，拒绝信息里带上当前压差和
  距目标带还差多少；
* 均衡被中止（``aborted``）——超时、轮次用尽未收敛（压差策略）、用尽时压差
  仍越带（轮次策略）或人工中止，都明确落在这档，充电不能把中止当成通过；
* 换电芯导致判据作废——回到 ``idle``，旧的完成结论作废，必须按新额定值
  重新跑一遍，不能拿旧结论放行。

终态原因随状态一起记："怎么完成/中止的"和"在不在完成档"同样重要。
"""

from __future__ import annotations

import threading

STATES = ("idle", "running", "complete", "aborted")


class BalanceState:
    """带计数的均衡状态机。"""

    def __init__(self) -> None:
        self._state = "idle"
        self._terminal_reason: str | None = None
        self._runs = 0
        self._completions = 0
        self._aborts = 0
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
                self._terminal_reason = None
            elif target == "complete":
                self._completions += 1
                self._terminal_reason = reason
            elif target == "aborted":
                self._aborts += 1
                self._terminal_reason = reason
            elif target == "idle":
                self._terminal_reason = None
            self._history.append({"from": previous, "to": target, "reason": reason})
            return self._state

    def is_complete(self) -> bool:
        """均衡是否已完成。"""

        return self.state == "complete"

    def terminal_reason(self) -> str | None:
        """返回最近一次进入终态（完成/中止）的原因。"""

        with self._lock:
            return self._terminal_reason

    def counts(self) -> dict:
        """返回运行、完成与中止计数。"""

        with self._lock:
            return {"runs": self._runs, "completions": self._completions, "aborts": self._aborts}

    def history(self, limit: int = 20) -> list[dict]:
        """返回最近的迁移记录。"""

        with self._lock:
            return [dict(item) for item in self._history[-limit:]]
