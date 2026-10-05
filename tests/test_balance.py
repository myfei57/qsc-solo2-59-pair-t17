"""均衡目标选择、节拍、状态机与充电联锁。"""

from __future__ import annotations

import pytest

from batterystore.balance.select import cell_delta_mv, select_cells


def test_balance_restores_delta_to_target(platform):
    platform.disturb_cell(cell_index=0, delta_v=0.06)
    assert platform.racks["rk-01"].metrics().delta_mv > platform.config.balance.target_delta_mv
    report = platform.balance(max_steps=600)
    assert report["ok"], report["violations"]
    assert platform.balancer.is_complete()
    assert platform.balancer.delta_mv() <= platform.config.balance.target_delta_mv
    snapshot = platform.balance_snapshot()
    assert snapshot["state"] == "complete"
    assert snapshot["state_reason"] == "delta_within_target"
    assert snapshot["counts"]["completions"] >= 1
    assert snapshot["run"]["cycle_s"] > 0
    assert snapshot["run"]["max_pulses"] == platform.config.balance.max_pulses


def test_select_returns_empty_when_delta_within_target(platform):
    cells = platform.racks["rk-01"].cells()
    assert cell_delta_mv(cells) == pytest.approx(0.0)
    assert select_cells(cells, platform.config.balance.target_delta_mv, 2) == []


def test_select_tops_up_to_minimum_cells(platform):
    cells = platform.racks["rk-01"].cells()
    cells[5].update(voltage_v=cells[5].voltage_v + 0.1)
    chosen = select_cells(cells, platform.config.balance.target_delta_mv, 3)
    assert len(chosen) == 3
    assert cells[5].cell_id in chosen


def test_balance_aborts_on_overrun(platform):
    rack = platform.racks["rk-01"]
    rack.cells()[0].update(voltage_v=rack.cells()[0].voltage_v + 0.5)
    platform.balancer.start(rack.cells(), rating_epoch=rack.rating_epoch)
    for _ in range(5):
        platform.balancer.advance(platform.config.balance.max_duration_s, rack.cells())
    assert platform.balancer.state.state == "aborted"
    assert platform.balancer.state.reason_for("aborted") == "duration_exceeded"
    assert platform.balancer.snapshot().run["overrun"] is True
    assert platform.balancer.reset()["previous"] == "aborted"


def test_balance_aborts_when_pulses_exhausted_without_converging(platform):
    """兜底判据：轮次用尽仍越限是中止，绝不能算完成放行。"""

    rack = platform.racks["rk-01"]
    # 0.8V 失配：每拍最多拉回 2mV，300 个脉冲收不进 25mV 目标带。
    rack.cells()[0].update(voltage_v=rack.cells()[0].voltage_v + 0.8)
    limits = platform.config.balance
    platform.balancer.start(rack.cells(), rating_epoch=rack.rating_epoch)
    # 脉冲/静置交替，脉冲只在奇数拍发生；小步长保证时长上限不先触发。
    for _ in range(limits.max_pulses * 2):
        platform.balancer.advance(1.0, rack.cells())
        if platform.balancer.state.state == "aborted":
            break
    assert platform.balancer.state.state == "aborted"
    assert platform.balancer.state.reason_for("aborted") == "pulses_exhausted"
    snapshot = platform.balance_snapshot()
    assert snapshot["run"]["pulses_exhausted"] is True
    assert snapshot["run"]["pulses_remaining"] == 0
    assert snapshot["delta_mv"] > limits.target_delta_mv
    gate = platform.balancer.gate(rack.rating_epoch)
    assert gate["passed"] is False
    assert gate["reason"] == "balance_aborted"
    assert gate["abort_reason"] == "pulses_exhausted"
    # 复位后重跑，确认中止状态不会自己变成完成。
    assert platform.balancer.reset()["previous"] == "aborted"


def test_charging_blocked_until_balance_complete(platform):
    platform.disturb_cell(cell_index=0, delta_v=0.08)
    platform.sample_temperature(temperature_c=30.0, dt=1.0)
    blocked = platform.charge(steps=2, dt=60.0)
    assert not blocked["ok"]
    assert "balance_incomplete" in blocked["violations"]


def test_gate_reports_delta_and_gap_while_running(platform):
    """没跑完时要能说清当前压差多少、距目标还差多少、还剩多少轮次。"""

    rack = platform.racks["rk-01"]
    platform.disturb_cell(cell_index=0, delta_v=0.08)
    platform.balancer.start(rack.cells(), rating_epoch=rack.rating_epoch)
    platform.balancer.advance(2.0, rack.cells())
    gate = platform.balancer.gate(rack.rating_epoch)
    assert gate["passed"] is False
    assert gate["state"] == "running"
    assert gate["reason"] == "balance_incomplete"
    assert gate["delta_mv"] > platform.config.balance.target_delta_mv
    assert gate["remaining_mv"] == pytest.approx(
        gate["delta_mv"] - gate["target_delta_mv"]
    )
    assert gate["pulses_remaining"] == platform.config.balance.max_pulses - 1


def test_charging_rejects_aborted_balance_as_not_passed(platform):
    """中止不是完成：充电不能把中止当通过。"""

    rack = platform.racks["rk-01"]
    rack.cells()[0].update(voltage_v=rack.cells()[0].voltage_v + 0.8)
    report = platform.balance(dt=1.0, max_steps=600)
    assert not report["ok"]
    assert platform.balancer.state.state == "aborted"
    assert platform.balance_snapshot()["state_reason"] == "pulses_exhausted"
    platform.sample_temperature(temperature_c=30.0, dt=1.0)
    blocked = platform.charge(steps=1, dt=60.0)
    assert not blocked["ok"]
    assert "balance_aborted" in blocked["violations"]
    # 复位并重新跑到收敛后才放行。
    platform.balancer.reset()
    rack.cells()[0].update(voltage_v=rack.cells()[0].voltage_v + 0.01)
    rerun = platform.balance(max_steps=600)
    assert rerun["ok"], rerun["violations"]
    assert platform.charge(steps=1, dt=60.0)["ok"]


def test_cell_swap_invalidates_balance_and_restarts_on_new_rating(platform):
    """均衡期间混入换芯：旧判据作废，按新额定值重新开始，旧结论不得放行。"""

    rack = platform.racks["rk-01"]
    platform.disturb_cell(cell_index=0, delta_v=0.06)
    first = platform.balance(max_steps=600)
    assert first["ok"]
    assert platform.balancer.gate(rack.rating_epoch)["passed"]
    replaced = platform.replace_pack_rating(pack_index=0, rating_v=3.35)
    invalidated = replaced["steps"][2]["detail"]
    assert invalidated["previous"] == "complete"
    assert invalidated["bound_rating_epoch"] == rack.rating_epoch - 1
    assert invalidated["current_rating_epoch"] == rack.rating_epoch
    gate = platform.balancer.gate(rack.rating_epoch)
    assert gate["passed"] is False
    assert gate["reason"] == "balance_rating_invalidated"
    assert platform.balancer.state.state == "aborted"
    assert platform.balancer.state.reason_for("aborted") == "rating_invalidated"
    assert platform.balance_snapshot()["rating_epoch"] == rack.rating_epoch - 1
    # 按新额定值重新均衡后才放行。
    rerun = platform.balance(max_steps=600)
    assert rerun["ok"], rerun["violations"]
    assert platform.balancer.gate(rack.rating_epoch)["passed"]
