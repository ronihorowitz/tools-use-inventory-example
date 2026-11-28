"""Deterministic arithmetic, checked against the prompt's examples and the stage 1 fixtures."""
from datetime import date
from decimal import Decimal

import pytest

from conftest import FIXTURE, SALES, sales_md5
from inventory_tools import (basket_cost, demand_from_sales, eoq, evaluate_order_quantities, plan_orders,
                             project_stockout, reorder_point, round_up_to_pack, unit_price_for)

MARCH_1 = date(2026, 3, 1)


def sent_order(supplier_id, lines, arrival, total='0', status='sent'):
    return {'id': f'order-{supplier_id}', 'supplier_id': supplier_id, 'status': status, 'request_key': 'k',
            'total': total, 'expected_arrival': arrival, 'lines': [{'sku': s, 'qty': q} for s, q in lines]}


# ---------------------------------------------------------------- fixtures match the database

def test_regenerated_sales_match_the_database():
    assert len(SALES) == 2160
    assert sales_md5(SALES) == FIXTURE['sales_md5'], 'conftest sales formula drifted from migration 002'
    spike = next(r for r in SALES if r['note'])
    assert spike['id'] == FIXTURE['pen_spike_sale_id'] and spike['qty'] == 906


# ---------------------------------------------------------------- EOQ and rounding

def test_eoq_textbook_example():
    assert eoq(1000, 10, 2) == Decimal(100)


def test_eoq_zero_demand_and_bad_inputs():
    assert eoq(0, 10, 2) == 0
    with pytest.raises(ValueError):
        eoq(-1, 10, 2)
    with pytest.raises(ValueError):
        eoq(100, 10, 0)


def test_rounding_respects_moq_and_packs():
    assert round_up_to_pack(Decimal('483.3'), 20, 10) == 490
    assert round_up_to_pack(Decimal(5), 20, 10) == 20     # MOQ wins
    assert round_up_to_pack(Decimal(25), 10, 10) == 30    # whole packs
    assert round_up_to_pack(Decimal(100), 100, 50) == 100


def test_all_unit_price_break():
    breaks = [{'min_qty': 500, 'unit_price': 4.8}]
    assert unit_price_for(5, breaks, 499) == Decimal(5)
    assert unit_price_for(5, breaks, 500) == Decimal('4.8')


def test_price_break_can_beat_the_rounded_eoq(snap):
    s = snap('budget')
    toner = evaluate_order_quantities(s.demand('TONER')['annual_demand'], s.suppliers['tech'], s.offers[('tech', 'TONER')],
                                      s.breaks[('tech', 'TONER')], s.products['TONER']['holding_rate'])
    reasons = {c['qty']: c['reasons'] for c in toner['candidates']}
    assert 'rounded EOQ' in reasons[55] and 'price break 100+' in reasons[100]
    assert toner['recommended']['qty'] == 100 and toner['recommended']['unit_price'] == Decimal(114)
    paper = evaluate_order_quantities(s.demand('PAP-A4')['annual_demand'], s.suppliers['north'], s.offers[('north', 'PAP-A4')],
                                      s.breaks[('north', 'PAP-A4')], s.products['PAP-A4']['holding_rate'])
    assert paper['recommended']['qty'] == 500 and paper['recommended']['unit_price'] == Decimal('4.8')


def test_zero_demand_recommends_nothing(snap):
    s = snap()
    result = evaluate_order_quantities(0, s.suppliers['north'], s.offers[('north', 'PAP-A4')], [], '0.25')
    assert result['recommended'] is None and result['candidates'] == []


def test_supplier_minimum_marks_small_candidates_infeasible(snap):
    s = snap()
    # 1 chair a year: the EOQ (about 1.3) rounds up to the MOQ of 2, i.e. $360, below Tech's $500 minimum
    result = evaluate_order_quantities(1, s.suppliers['tech'], s.offers[('tech', 'CHAIR')], [], '0.2')
    small = next(c for c in result['candidates'] if c['qty'] == 2)
    assert not small['feasible'] and 'below supplier minimum order value' in small['problems']
    assert result['recommended']['merchandise'] >= Decimal(500)


# ---------------------------------------------------------------- demand and exclusions

def test_paper_demand_is_exactly_twenty_per_day(snap):
    d = snap().demand('PAP-A4')
    assert d['daily_demand'] == Decimal(20) and d['annual_demand'] == Decimal(7300)
    assert (d['window_start'], d['window_end']) == (date(2025, 12, 1), date(2026, 2, 28))


def test_excluding_the_annotated_spike_removes_the_whole_day(snap):
    s = snap('anomaly')
    before, after = s.demand('PEN-BLU'), s.demand('PEN-BLU', [1606])
    assert after['excluded_units'] == 906                      # 900 extra + that day's 6 ordinary boxes
    assert before['units'] - after['units'] == 906
    assert after['daily_demand'] == Decimal(after['units']) / 90  # denominator stays 90


def test_only_annotated_in_window_sales_of_that_sku_can_be_excluded(snap):
    s = snap('anomaly')
    with pytest.raises(ValueError, match='annotated'):
        s.demand('PEN-BLU', [1605])                            # in window, but no note
    with pytest.raises(ValueError, match='annotated'):
        s.demand('PAP-A4', [1606])                             # another SKU's sale
    rows = [{'id': 1, 'sku': 'X', 'sale_date': '2025-11-01', 'qty': 50, 'note': 'one-off'}]
    with pytest.raises(ValueError, match='annotated'):
        demand_from_sales(rows, MARCH_1, [1])                  # annotated but outside the window


# ---------------------------------------------------------------- reorder point and stockouts

def test_reorder_point():
    assert reorder_point(20, 2, 40) == Decimal(80)
    with pytest.raises(ValueError):
        reorder_point(20, 0, 40)


def test_stockout_without_and_with_a_scheduled_receipt():
    assert project_stockout(160, 20, MARCH_1)['stockout_date'] == date(2026, 3, 9)
    with_receipt = project_stockout(160, 20, MARCH_1, [('2026-03-04', 100)])
    assert with_receipt['stockout_date'] == date(2026, 3, 14)


def test_zero_demand_never_runs_out():
    assert project_stockout(0, 0, MARCH_1)['stockout_date'] is None


def test_sent_orders_count_as_receipts_but_drafts_do_not(snap):
    s = snap(orders=[sent_order('west', [('PAP-A4', 100)], '2026-03-04'),
                     sent_order('north', [('PAP-A4', 50)], '2026-03-03', status='draft')])
    assert s.receipts('PAP-A4') == [('2026-03-04', 100)]


# ---------------------------------------------------------------- basket cost and validation

def test_consolidation_750_versus_620(snap):
    s = snap('consolidation')
    separate = sum(basket_cost(s, 'desk', [{'sku': sku, 'qty': 100}])['total'] for sku in ('PEN-BLU', 'NOTE-A5', 'MARK-BLK'))
    combined = basket_cost(s, 'desk', [{'sku': sku, 'qty': 100} for sku in ('PEN-BLU', 'NOTE-A5', 'MARK-BLK')])
    assert separate == Decimal(750)
    assert combined['total'] == Decimal(620) and combined['shipping_fee'] == 0
    assert separate - combined['total'] == Decimal(130)


def test_budget_example_costs(snap):
    s = snap('budget')
    toner = basket_cost(s, 'tech', [{'sku': 'TONER', 'qty': 100}])['total']
    chairs = basket_cost(s, 'tech', [{'sku': 'CHAIR', 'qty': 50}])['total']
    assert (toner, chairs) == (Decimal(11430), Decimal(8580)) and toner + chairs > 20000


@pytest.mark.parametrize('qty, message', [(20.5, 'whole units'), (0, 'whole units'), (-10, 'whole units'),
                                          (True, 'whole units'), ('many', 'whole units'), (25, 'packs of 10'),
                                          (10, 'MOQ')])
def test_invalid_quantities_are_rejected(snap, qty, message):
    with pytest.raises(ValueError, match=message):
        basket_cost(snap(), 'west', [{'sku': 'PAP-A4', 'qty': qty}])


def test_other_invalid_orders(snap):
    s = snap()
    with pytest.raises(ValueError, match='Duplicate'):
        basket_cost(s, 'west', [{'sku': 'PAP-A4', 'qty': 20}, {'sku': 'PAP-A4', 'qty': 30}])
    with pytest.raises(ValueError, match='not active'):
        basket_cost(s, 'west', [{'sku': 'PEN-BLU', 'qty': 20}])
    with pytest.raises(ValueError, match='does not supply'):
        basket_cost(s, 'desk', [{'sku': 'PAP-A4', 'qty': 20}])
    with pytest.raises(ValueError, match='Unknown supplier'):
        basket_cost(s, 'nobody', [{'sku': 'PAP-A4', 'qty': 20}])
    with pytest.raises(ValueError, match='minimum order value'):
        basket_cost(snap('consolidation'), 'desk', [{'sku': 'PEN-BLU', 'qty': 20}])


def test_cumulative_cap_holds_across_split_orders(snap):
    with pytest.raises(ValueError, match='cumulative quantity exceeds trusted scenario limits'):
        basket_cost(snap('injection'), 'north', [{'sku': 'PAP-A4', 'qty': 10000}])
    after_700 = snap('injection', orders=[sent_order('north', [('PAP-A4', 700)], '2026-03-03', total='3380', status='draft')])
    with pytest.raises(ValueError, match='would reach 1400 units against a cap of 1200'):
        basket_cost(after_700, 'north', [{'sku': 'PAP-A4', 'qty': 700}])
    assert basket_cost(after_700, 'north', [{'sku': 'PAP-A4', 'qty': 500}])['total'] == Decimal('2420.00')


def test_budget_flag_counts_existing_orders(snap):
    s = snap('budget', orders=[sent_order('tech', [('TONER', 100)], '2026-03-05', total='11430', status='draft')])
    chairs = basket_cost(s, 'tech', [{'sku': 'CHAIR', 'qty': 50}])
    assert chairs['remaining_budget_before'] == Decimal(8570) and not chairs['within_budget']


# ---------------------------------------------------------------- planning

def test_disruption_supplier_switch_follows_the_evidence(snap):
    s = snap('disruption')
    normal = plan_orders(s)
    assert [o['supplier_id'] for o in normal['orders']] == ['north']
    delayed = plan_orders(s, {'north': 21})
    assert [o['supplier_id'] for o in delayed['orders']] == ['west']
    assert delayed['orders'][0]['expected_arrival'] == date(2026, 3, 4)
    assert s.suppliers['north']['lead_time_days'] == 2         # master data untouched


def test_budget_plan_includes_toner_and_chairs_within_budget(snap):
    result = plan_orders(snap('budget'))
    planned = {line['sku'] for order in result['orders'] for line in order['lines']}
    assert {'TONER', 'CHAIR'} <= planned
    assert result['total'] <= Decimal(20000)
    assert any('TAPE' in w and 'unavoidable' in w for w in result['warnings'])
    assert 'not a global optimum' in result['note']


def test_excluding_the_spike_removes_the_pen_shortage(snap):
    s = snap('anomaly')
    assert [l['sku'] for o in plan_orders(s)['orders'] for l in o['lines']] == ['PEN-BLU']
    assert plan_orders(s, excluded_sales_ids=[1606])['orders'] == []


def test_override_validation(snap):
    with pytest.raises(ValueError):
        plan_orders(snap(), {'north': 0})
    with pytest.raises(ValueError, match='Unknown supplier'):
        plan_orders(snap(), {'nobody': 3})
