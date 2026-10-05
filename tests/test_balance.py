"""均衡目标选择、节拍、双判据状态机与充电联锁。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from batterystore.balance.select import cell_delta_mv, select_cells


def _balance_config(config, **overrides):
    """替换均衡配置段，其余段不动。"""

    return replace(config, balance=replace(config.balance, **overrides))


def _rebuild(platform, **overrides):
    """用新的均衡配置换一个 Balancer。"""

    from batterystore.balance.balance import Balancer

    platform.config = _balance_config(platform.config, **overrides)
    platform.balancer = Balancer(platform.config.balance)
    return platform


def test_balance_restores_delta_to_target(platform):
    platform.disturb_cell(cell_index=0, delta_v=0.06)
    assert platform.racks["rk-01"].metrics().delta_mv > platform.config.balance.target_delta_mv
    report = platform.balance(max_steps=600)
    assert report["ok"], report["violations"]
    assert platform.balancer.is_complete()
    assert platform.balancer.delta_mv() <= platform.config.balance.target_delta_mv
    snapshot = platform.balance_snapshot()
    assert snapshot["state"] == "complete"
    assert snapshot["terminal_reason"] == "delta_within_target"
    assert snapshot["counts"]["completions"] >= 1
    assert snapshot["run"]["cycle_s"] > 0


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
    platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    for _ in range(5):
        platform.balancer.advance(platform.config.balance.max_duration_s, rack.cells())
    assert platform.balancer.state.state == "aborted"
    assert platform.balancer.state.terminal_reason() == "duration_exceeded"
    assert platform.balancer.snapshot().run["overrun"] is True
    assert platform.balancer.reset()["previous"] == "aborted"


def test_charging_blocked_until_balance_complete(platform):
    platform.disturb_cell(cell_index=0, delta_v=0.08)
    platform.sample_temperature(temperature_c=30.0, dt=1.0)
    blocked = platform.charge(steps=2, dt=60.0)
    assert not blocked["ok"]
    # 没跑过均衡时，拒绝原因必须精确到 balance_not_run，而不是笼统的 incomplete。
    assert "balance_not_run" in blocked["violations"]


def test_charge_gate_reports_current_delta_and_remaining(platform):
    """没完成时必须说清现在压差多少、还差多少。"""

    rack = platform.racks["rk-01"]
    platform.disturb_cell(cell_index=0, delta_v=0.06)
    platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_running"
    assert gate["delta_mv"] == pytest.approx(60.0)
    assert gate["remaining_mv"] == pytest.approx(35.0)
    assert gate["target_delta_mv"] == platform.config.balance.target_delta_mv
    # 充电编排要把这份信息带进审计步骤。
    blocked = platform.charge(steps=1)
    gate_steps = [step["detail"] for step in blocked["steps"] if step["action"] == "charge_gate"]
    assert gate_steps and gate_steps[0]["remaining_mv"] == pytest.approx(35.0)


def test_aborted_balance_does_not_pass_charge_gate(platform):
    """中止就是中止，充电不能把中止当完成。"""

    rack = platform.racks["rk-01"]
    platform.disturb_cell(cell_index=0, delta_v=0.05)
    platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    result = platform.abort_balance()
    assert result["ok"]
    assert platform.balancer.state.state == "aborted"
    assert platform.balancer.state.terminal_reason() == "manual_abort"
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_aborted"
    blocked = platform.charge(steps=1)
    assert not blocked["ok"]
    assert "balance_aborted" in blocked["violations"]
    assert "balance_incomplete" not in blocked["violations"]


def test_delta_policy_aborts_when_pulse_budget_exhausted_non_converging(platform):
    """压差策略（默认）：脉冲轮次用尽仍不收敛 → 中止，不是完成。"""

    _rebuild(platform, completion_policy="delta", max_pulses=3)
    rack = platform.racks["rk-01"]
    rack.cells()[0].update(voltage_v=rack.cells()[0].voltage_v + 0.5)
    platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    beats = 0
    while platform.balancer.state.state == "running":
        platform.balancer.advance(1.0, rack.cells())
        beats += 1
        assert beats < 50
    assert platform.balancer.state.state == "aborted"
    assert platform.balancer.state.terminal_reason() == "pulse_budget_exceeded_non_converging"
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_aborted"


def test_pulses_policy_completes_when_delta_within_band(platform):
    """轮次策略兜底：轮次用尽且压差已回带 → 完成，可充电。"""

    _rebuild(platform, completion_policy="pulses", max_pulses=200)
    platform.disturb_cell(cell_index=0, delta_v=0.02)
    report = platform.balance(max_steps=500)
    assert report["ok"], report["violations"]
    assert platform.balancer.state.state == "complete"
    rack = platform.racks["rk-01"]
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is True
    assert platform.balancer.delta_mv() <= platform.config.balance.target_delta_mv


def test_pulses_policy_aborts_when_exhausted_but_delta_still_high(platform):
    """轮次策略：轮次用尽但低压单体没补平（压差仍越带）→ 中止，不放行。"""

    _rebuild(platform, completion_policy="pulses", max_pulses=2)
    rack = platform.racks["rk-01"]
    rack.cells()[0].update(voltage_v=rack.cells()[0].voltage_v + 0.5)
    report = platform.balance(max_steps=50)
    assert not report["ok"]
    assert any(item.startswith("balance_aborted:") for item in report["violations"])
    assert platform.balancer.state.state == "aborted"
    assert platform.balancer.state.terminal_reason() == "pulse_budget_exhausted_delta_high"
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_aborted"


def test_completion_verdict_invalidated_by_pack_replacement(platform):
    """均衡期间/之后换芯：旧结论作废，按新额定值重跑前充电拒绝。"""

    rack = platform.racks["rk-01"]
    platform.disturb_cell(cell_index=0, delta_v=0.03)
    done = platform.balance(max_steps=600)
    assert done["ok"], done["violations"]
    assert platform.balancer.is_complete()
    gate_before = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate_before["passed"] is True

    replaced = platform.replace_pack_rating(pack_index=0, rating_v=3.35)
    verdict = replaced["steps"][0]["detail"]["balance_verdict"]
    # 旧的完成结论被明确作废，状态回到 idle，而不是 aborted/complete。
    assert verdict["previous"] == "complete"
    assert verdict["state"] == "idle"
    assert platform.balancer.state.state == "idle"
    assert platform.balance_snapshot()["invalidated_reason"] == "pack_rating_replaced"

    # 不能拿旧结论放行：判据已作废回到未运行态，门明确拒绝。
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_not_run"
    blocked = platform.charge(steps=1)
    assert not blocked["ok"]
    assert "balance_not_run" in blocked["violations"]

    # 按新额定值重新均衡后才能充电。
    redone = platform.balance(rack_id=rack.rack_id, max_steps=600)
    assert redone["ok"], redone["violations"]
    assert platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())["passed"] is True


def test_swap_during_running_balance_invalidates_mid_run(platform):
    """均衡跑到一半换芯：进行中的判据立即作废，不计完成也不计中止。"""

    rack = platform.racks["rk-01"]
    platform.disturb_cell(cell_index=0, delta_v=0.05)
    platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    assert platform.balancer.state.state == "running"
    platform.replace_pack_rating(pack_index=1, rating_v=3.3)
    assert platform.balancer.state.state == "idle"
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_not_run"


def test_gate_flags_stale_epoch_when_verdict_not_explicitly_invalidated(platform):
    """完成结论还在、但外部直接抬了额定值代际：门按代际判过期，拒绝放行。"""

    rack = platform.racks["rk-01"]
    report = platform.balance(rack_id=rack.rack_id, max_steps=600)
    assert report["ok"]
    # 模拟外部直接改额定值（绕过平台换芯编排）：结论仍在 complete 档。
    rack._rating_epoch += 1
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_verdict_stale_rating_epoch"


def test_balance_binds_to_rack_and_rejects_other_rack_verdict(platform):
    """在 rk-01 上跑出来的完成结论不能拿去给 rk-02 放行。"""

    report = platform.balance(rack_id="rk-01", max_steps=600)
    assert report["ok"]
    other = platform.racks["rk-02"]
    gate = platform.balancer.gate(other.rack_id, other.rating_epoch, other.cells())
    assert gate["passed"] is False
    assert gate["reason"] == "balance_verdict_other_rack"


def test_start_skips_run_when_delta_already_within_target(platform):
    """启动前压差已在目标带：直接完成，不烧脉冲轮次。"""

    rack = platform.racks["rk-01"]
    started = platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    assert started["started"] is False
    assert platform.balancer.state.state == "complete"
    assert platform.balancer.state.terminal_reason() == "delta_within_target"
    assert platform.balancer.snapshot().as_dict()["run"]["pulses"] == 0
