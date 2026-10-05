"""BMS 就绪、闩锁、保护与告警路由。"""

from __future__ import annotations

import pytest

from batterystore.errors import InterlockError


def test_ready_gate_blocks_over_temperature(platform):
    rack = platform.racks["rk-01"]
    rack.apply_temperature(platform.config.bms.ready_temp_c + 5.0)
    report = platform.bms.ready(rack)
    assert not report.ready
    assert "temperature_high" in report.reasons
    with pytest.raises(InterlockError):
        platform.bms.require_ready(rack)


def test_fault_latch_requires_confirmation_rounds(platform):
    first = platform.report_fault("rk-01", "cell_over_temp")
    assert first["latched"] is False
    assert platform.bms.faults.counter("rk-01") == 1
    second = platform.report_fault("rk-01", "cell_over_temp")
    assert second["latched"] is True
    assert platform.bms.latched("rk-01")
    assert platform.bms.latch.generation("rk-01") == 1
    assert platform.bms.snapshot().pending_faults == []


def test_fault_latch_holds_then_expires(platform, clock):
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.bms.latched("rk-01")
    clock.advance(platform.config.bms.fault_latch_s + 1.0)
    assert not platform.bms.latched("rk-01")
    assert platform.bms.latch.active(clock.now()) == []


def test_clear_fault_releases_latch(platform):
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.clear_fault("rk-01")["cleared"] is True
    assert platform.bms.latched("rk-01") is False
    assert platform.clear_fault("rk-01")["cleared"] is False


def test_alarm_routing_uses_latest_label(platform):
    rack = platform.racks["rk-01"]
    rack.apply_temperature(platform.config.rack.temp_warn_c + 1.0)
    platform.mapping.assign("rk-01", "R77", "Z2")
    alerts = platform.alerts()
    assert alerts
    assert {alert["label"] for alert in alerts} == {"R77"}
    assert {alert["zone"] for alert in alerts} == {"Z2"}
    assert alerts[0]["mapping_epoch"] == 3


def test_protection_lists_violations(platform):
    rack = platform.racks["rk-01"]
    rack.apply_temperature(platform.config.bms.protection_temp_c + 1.0)
    assert "temperature_protection" in platform.bms.protection(rack)
    platform.cycle()
    assert platform.pcs.cut.is_tripped()


def test_charge_plan_blocks_before_balance(platform):
    rack = platform.racks["rk-01"]
    plan = platform.bms.charge_plan(
        rack,
        platform.pcs.limits,
        balance_complete=False,
        temp_derate_ratio=1.0,
        balance_min_delta_mv=platform.config.balance.target_delta_mv,
    )
    assert not plan.allowed
    assert "balance_incomplete" in plan.as_dict()["reasons"]


def test_charge_plan_uses_gate_verdict_and_surfaces_delta(platform):
    """带门结论构造计划：拒绝原因取门结论，计划里带压差与余量。"""

    rack = platform.racks["rk-01"]
    platform.disturb_cell(cell_index=0, delta_v=0.05)
    platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    plan = platform.bms.charge_plan(
        rack,
        platform.pcs.limits,
        balance_complete=gate["passed"],
        temp_derate_ratio=1.0,
        balance_min_delta_mv=platform.config.balance.target_delta_mv,
        balance_gate=gate,
    )
    payload = plan.as_dict()
    assert not plan.allowed
    assert payload["reasons"] == ["balance_running"]
    assert payload["balance_gate"]["remaining_mv"] == 25.0
    assert payload["balance_gate"]["delta_mv"] == 50.0


def test_charge_plan_passes_with_completed_gate_even_if_delta_rechecked(platform):
    """门通过（轮次兜底完成）时，裸压差不再二次拦截。"""

    rack = platform.racks["rk-01"]
    platform.balancer.start(rack.cells(), rack_id=rack.rack_id, rating_epoch=rack.rating_epoch)
    assert platform.balancer.state.state == "complete"
    gate = platform.balancer.gate(rack.rack_id, rack.rating_epoch, rack.cells())
    assert gate["passed"]
    plan = platform.bms.charge_plan(
        rack,
        platform.pcs.limits,
        balance_complete=False,
        temp_derate_ratio=1.0,
        balance_min_delta_mv=platform.config.balance.target_delta_mv,
        balance_gate=gate,
    )
    assert plan.allowed
