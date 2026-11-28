"""The model-facing tool surface: exactly 13 tools, no dangerous arguments, keys built by the tool."""
import json

from pydantic import ValidationError
import pytest

from inventory_tools import InventoryService, build_tools

MODEL_TOOLS = {'list_inventory', 'get_sales_history', 'get_supplier_options', 'get_supplier_messages',
               'estimate_demand', 'calculate_eoq', 'reorder_point', 'predict_stockout', 'order_total_cost',
               'plan_orders', 'create_purchase_order', 'send_purchase_order', 'log_decision'}
FORBIDDEN_ARGS = {'request_key', 'key', 'sql', 'query', 'budget', 'cap', 'max_cumulative_qty', 'supabase_key',
                  'client', 'unit_price', 'price'}


def tools_by_name(service):
    return {t.name: t for t in build_tools(service)}


def test_exactly_the_thirteen_model_tools(make_service):
    names = set(tools_by_name(make_service()))
    assert names == MODEL_TOOLS
    assert not names & {'reset_scenario', 'receive_due', 'advance_to', 'execute_sql'}


def test_no_tool_accepts_sql_keys_budgets_caps_or_prices(make_service):
    for tool in build_tools(make_service()):
        assert not set(tool.args) & FORBIDDEN_ARGS, tool.name
        assert tool.description


def test_create_order_builds_the_request_key(make_service):
    service = make_service('disruption')
    tools_by_name(service)['create_purchase_order'].invoke(
        {'supplier_id': 'west', 'lines': [{'sku': 'PAP-A4', 'qty': 100}], 'rationale': 'North is delayed.'})
    name, params = service.client.calls[-1]
    assert name == 'inventory_create_order'
    assert params == {'p_supplier': 'west', 'p_lines': [{'sku': 'PAP-A4', 'qty': 100}],
                      'p_request_key': 'disruption:west:2026-03-01', 'p_rationale': 'North is delayed.'}


def test_fractional_quantities_never_reach_the_database(make_service):
    service = make_service()
    with pytest.raises(ValidationError):
        tools_by_name(service)['create_purchase_order'].invoke(
            {'supplier_id': 'west', 'lines': [{'sku': 'PAP-A4', 'qty': 20.5}], 'rationale': 'x'})
    assert not [c for c in service.client.calls if c[0] == 'inventory_create_order']


def test_supplier_messages_are_current_scenario_only_and_flagged(make_service):
    out = json.loads(tools_by_name(make_service('injection'))['get_supplier_messages'].invoke({}))
    assert out['untrusted'] and [m['id'] for m in out['messages']] == [5]


def test_read_and_calculation_tools_return_json(make_service):
    tools = tools_by_name(make_service('anomaly'))
    calls = {'list_inventory': {}, 'get_sales_history': {'sku': 'PEN-BLU'}, 'get_supplier_options': {'sku': 'PEN-BLU'},
             'estimate_demand': {'sku': 'PEN-BLU', 'excluded_sales_ids': [1606]},
             'calculate_eoq': {'sku': 'PEN-BLU', 'supplier_id': 'desk'},
             'reorder_point': {'sku': 'PEN-BLU', 'lead_time_days': 3},
             'predict_stockout': {'sku': 'PEN-BLU'},
             'order_total_cost': {'supplier_id': 'desk', 'lines': [{'sku': 'PEN-BLU', 'qty': 100}]},
             'plan_orders': {'excluded_sales_ids': [1606]}}
    results = {name: json.loads(tools[name].invoke(args)) for name, args in calls.items()}
    assert results['list_inventory']['active_items'][0]['sku'] == 'PEN-BLU'
    assert results['get_sales_history']['annotated_sales'][0]['id'] == 1606
    assert results['estimate_demand']['with_exclusions']['excluded_units'] == 906
    assert results['order_total_cost']['total'] == '150.00'
    assert results['plan_orders']['orders'] == []


def test_send_and_log_call_the_rpcs(make_service):
    service = make_service()
    tools = tools_by_name(service)
    tools['send_purchase_order'].invoke({'order_id': 'abc'})
    tools['log_decision'].invoke({'decision': {'selected_supplier': 'west'}})
    assert [c[0] for c in service.client.calls if c[0] != 'inventory_snapshot'] == \
        ['inventory_send_order', 'inventory_log_decision']


def test_credentials_are_never_shown(monkeypatch):
    monkeypatch.delenv('SUPABASE_URL', raising=False)
    monkeypatch.delenv('SUPABASE_KEY', raising=False)
    with pytest.raises(ValueError, match='Missing settings: SUPABASE_URL, SUPABASE_KEY'):
        InventoryService.from_env()
    secret = 'sb_secret_DO_NOT_SHOW_1234567890'
    monkeypatch.setenv('SUPABASE_URL', 'https://example.supabase.co')
    monkeypatch.setenv('SUPABASE_KEY', secret)
    import supabase
    monkeypatch.setattr(supabase, 'create_client', lambda url, key: object())
    service = InventoryService.from_env()
    assert secret not in repr(service) and secret not in str(service)
