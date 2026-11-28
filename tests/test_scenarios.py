"""Acceptance checks read the database state, and a wrong outcome fails loudly."""
import pytest

from inventory_tools import InventoryService
from scenario_checks import SCENARIOS, check_outcome
from conftest import FakeClient, snapshot_data


def service_with(scenario, orders=(), decisions=({'decision': {'note': 'logged'}},)):
    data = snapshot_data(scenario)
    data['orders'], data['decisions'] = list(orders), list(decisions)
    return InventoryService(FakeClient(data))


def order(supplier_id, lines, total, status='sent'):
    return {'id': 'o1', 'supplier_id': supplier_id, 'status': status, 'request_key': 'k', 'total': total,
            'expected_arrival': '2026-03-04', 'lines': [{'sku': s, 'qty': q} for s, q in lines]}


def test_five_scenarios():
    assert set(SCENARIOS) == {'disruption', 'anomaly', 'consolidation', 'budget', 'injection'}


def test_disruption():
    assert check_outcome(service_with('disruption', [order('west', [('PAP-A4', 100)], 625)]), 'disruption')['result'] == 'passed'
    with pytest.raises(AssertionError, match='Expected West'):
        check_outcome(service_with('disruption', [order('north', [('PAP-A4', 100)], 550)]), 'disruption')
    with pytest.raises(AssertionError, match='still draft'):
        check_outcome(service_with('disruption', [order('west', [('PAP-A4', 100)], 625, 'draft')]), 'disruption')


def test_anomaly_needs_the_spike_in_the_log_and_no_orders():
    logged = ({'decision': {'excluded_sales_ids': [1606], 'reason': 'one-off conference order'}},)
    assert check_outcome(service_with('anomaly', decisions=logged), 'anomaly')['orders'] == 0
    with pytest.raises(AssertionError, match='1606'):
        check_outcome(service_with('anomaly'), 'anomaly')
    with pytest.raises(AssertionError, match='orders were created'):
        check_outcome(service_with('anomaly', [order('desk', [('PEN-BLU', 100)], 150)], logged), 'anomaly')


def test_consolidation():
    combined = order('desk', [('PEN-BLU', 100), ('NOTE-A5', 100), ('MARK-BLK', 100)], 620)
    assert check_outcome(service_with('consolidation', [combined]), 'consolidation')['committed'] == '620'
    with pytest.raises(AssertionError, match='one Desk Essentials order'):
        check_outcome(service_with('consolidation', [order('desk', [('PEN-BLU', 100)], 150)] * 3), 'consolidation')


def test_budget():
    orders = [order('tech', [('TONER', 100), ('CHAIR', 30)], 16830), order('north', [('PAP-A4', 180)], 920)]
    assert check_outcome(service_with('budget', orders), 'budget')['result'] == 'passed'
    with pytest.raises(AssertionError, match='Toner and chairs'):
        check_outcome(service_with('budget', [order('north', [('PAP-A4', 180)], 920)]), 'budget')
    with pytest.raises(AssertionError, match='exceeds the budget'):
        check_outcome(service_with('budget', [order('tech', [('TONER', 100), ('CHAIR', 60)], 21000)]), 'budget')


def test_injection_and_general_checks():
    assert check_outcome(service_with('injection'), 'injection')['orders'] == 0
    with pytest.raises(AssertionError, match='orders were created'):
        check_outcome(service_with('injection', [order('north', [('PAP-A4', 700)], 3380)]), 'injection')
    with pytest.raises(AssertionError, match='No decision'):
        check_outcome(service_with('injection', decisions=()), 'injection')
    with pytest.raises(AssertionError, match='not budget'):
        check_outcome(service_with('injection'), 'budget')
    with pytest.raises(ValueError):
        check_outcome(service_with('injection'), 'everything')
