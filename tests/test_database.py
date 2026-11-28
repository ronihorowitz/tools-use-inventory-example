"""PostgreSQL integration tests: the real migrations, RPCs and InventoryService together.

They need TEST_DATABASE_URL pointing at an EMPTY, disposable PostgreSQL database (CI provides one),
never the instructor project. They refuse a database that already has an `inventory` schema, apply
migrations 001-004, and drop everything again at the end.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time

import pytest

URL = os.getenv('TEST_DATABASE_URL')
if not URL:
    pytest.skip('Set TEST_DATABASE_URL to an EMPTY disposable PostgreSQL database to run these tests',
                allow_module_level=True)
try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError as error:  # e.g. the compiled driver is blocked on this machine
    pytest.skip(f'psycopg cannot load here: {str(error).splitlines()[0]}', allow_module_level=True)

from inventory_tools import InventoryService  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / 'supabase' / 'migrations').glob('*.sql'))
RPCS = {'inventory_snapshot', 'inventory_reset_scenario', 'inventory_create_order', 'inventory_send_order',
        'inventory_receive_due', 'inventory_log_decision'}


class PgClient:
    """Minimal stand-in for supabase-py's client.rpc(...).execute().data, run as service_role."""

    def __init__(self, conn):
        self.conn = conn

    def rpc(self, name, params):
        assert name in RPCS
        args = ', '.join(f'{k} => %({k})s' for k in params)
        values = {k: Jsonb(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
        with self.conn.transaction():
            self.conn.execute('set local role service_role')
            data = self.conn.execute(f'select public.{name}({args})', values).fetchone()[0]

        class Builder:
            def execute(self):
                class Response:
                    pass
                response = Response()
                response.data = data
                return response
        return Builder()


def connect():
    return psycopg.connect(URL, autocommit=True)


@pytest.fixture(scope='module')
def db():
    with connect() as conn:
        if conn.execute("select 1 from pg_namespace where nspname = 'inventory'").fetchone():
            pytest.fail('TEST_DATABASE_URL already has an inventory schema; use an EMPTY disposable database')
        # The roles Supabase provides, with Supabase's attributes: service_role bypasses RLS
        # (the tables have RLS on and no policies), anon and authenticated do not.
        for role, attributes in (('anon', 'nologin'), ('authenticated', 'nologin'), ('service_role', 'nologin bypassrls')):
            conn.execute(f"do $$ begin if not exists (select from pg_roles where rolname = '{role}') "
                         f"then create role {role} {attributes}; end if; end $$")
            conn.execute(f'alter role {role} {attributes}')
        for migration in MIGRATIONS:
            conn.execute(migration.read_text(encoding='utf-8'))
        yield conn
        conn.execute('drop schema inventory, inventory_checkpoints cascade')
        for (signature,) in conn.execute("select p.oid::regprocedure::text from pg_proc p join pg_namespace n "
                                         "on n.oid = p.pronamespace where n.nspname = 'public' "
                                         "and p.proname like 'inventory\\_%'").fetchall():
            conn.execute(f'drop function {signature}')


@pytest.fixture
def service(db):
    return InventoryService(PgClient(db))


def order_count(db):
    return db.execute('select count(*) from inventory.purchase_orders').fetchone()[0]


def test_migrations_and_fixture_counts(db):
    counts = db.execute('select (select count(*) from inventory.products), (select count(*) from inventory.sales_history), '
                        '(select max(v) from inventory_checkpoints.checkpoint_migrations)').fetchone()
    assert counts == (12, 2160, 9)


def test_database_assertion_script_passes_and_leaves_no_trace(db):
    before = db.execute('select run_id from inventory.demo_state').fetchone()[0]
    with connect() as conn:  # its own session: the script opens and rolls back a transaction
        conn.execute((ROOT / 'tests' / 'database_assertions.sql').read_text(encoding='utf-8'))
    assert db.execute('select run_id from inventory.demo_state').fetchone()[0] == before
    assert order_count(db) == 0


def test_python_costs_match_the_database(service):
    cases = [('consolidation', 'desk', [{'sku': s, 'qty': 100} for s in ('PEN-BLU', 'NOTE-A5', 'MARK-BLK')], '620.00'),
             ('budget', 'tech', [{'sku': 'TONER', 'qty': 100}], '11430.00'),
             ('disruption', 'west', [{'sku': 'PAP-A4', 'qty': 100}], '625.00'),
             ('injection', 'north', [{'sku': 'PAP-A4', 'qty': 700}], '3380.00')]
    for scenario, supplier_id, lines, expected in cases:
        service.reset_scenario(scenario)
        python_total = service.order_total_cost(supplier_id, lines)['total']
        order = service.create_purchase_order(supplier_id, lines, 'integration test')
        assert python_total == expected == f'{float(order["total"]):.2f}', scenario


def test_python_validation_matches_database_errors(service):
    service.reset_scenario('injection')
    service.create_purchase_order('north', [{'sku': 'PAP-A4', 'qty': 700}], 'first')
    for check in (lambda: service.order_total_cost('west', [{'sku': 'PAP-A4', 'qty': 700}]),
                  lambda: service.create_purchase_order('west', [{'sku': 'PAP-A4', 'qty': 700}], 'split')):
        with pytest.raises(Exception, match='cumulative quantity exceeds trusted scenario limits'):
            check()


def test_failed_order_rolls_back_completely(db, service):
    service.reset_scenario('disruption')
    with pytest.raises(Exception, match='packs of 10'):
        service.create_purchase_order('west', [{'sku': 'PAP-A4', 'qty': 25}], 'bad pack')
    assert order_count(db) == 0
    assert db.execute('select count(*) from inventory.po_lines').fetchone()[0] == 0


def test_idempotent_retry_through_the_service(db, service):
    service.reset_scenario('disruption')
    first = service.create_purchase_order('west', [{'sku': 'PAP-A4', 'qty': 100}], 'North is delayed.')
    again = service.create_purchase_order('west', [{'sku': 'PAP-A4', 'qty': 100}], 'Retry after a timeout.')
    assert again['id'] == first['id'] and again['idempotent_replay'] is True
    assert first['request_key'] == 'disruption:west:2026-03-01'
    with pytest.raises(Exception, match='already used'):
        service.create_purchase_order('west', [{'sku': 'PAP-A4', 'qty': 120}], 'Changed my mind.')
    assert order_count(db) == 1


def test_send_receive_and_snapshot_through_the_service(service):
    service.reset_scenario('disruption')
    order = service.create_purchase_order('west', [{'sku': 'PAP-A4', 'qty': 100}], 'North is delayed.')
    service.send_purchase_order(order['id'])
    assert service.predict_stockout('PAP-A4')['scheduled_receipts'] == [{'date': '2026-03-04', 'qty': 100}]
    service.receive_due('2026-03-04')
    inventory = service.list_inventory()
    assert inventory['active_items'][0]['on_hand'] == 260 and inventory['as_of'] == '2026-03-04'
    assert service.log_decision({'selected_supplier': 'west'})['decision_id'] > 0


def test_concurrent_orders_cannot_overspend(db):
    """A holds the scenario lock while B tries to spend; B must wait and then see A's spending."""
    db.execute("select public.inventory_reset_scenario('budget')")
    holding, results = threading.Event(), {}

    def order_a():
        with connect() as conn, conn.transaction():
            conn.execute('''select public.inventory_create_order('tech', '[{"sku": "TONER", "qty": 100}]', 'race-a')''')
            holding.set()
            time.sleep(1.5)                      # keep the lock while B starts
        results['a'] = 'ok'

    def order_b():
        holding.wait(10)
        started = time.monotonic()
        try:
            with connect() as conn:
                conn.execute('''select public.inventory_create_order('tech', '[{"sku": "CHAIR", "qty": 50}]', 'race-b')''')
            results['b'] = 'ok'
        except psycopg.Error as error:
            results['b'] = str(error)
        results['b_waited'] = time.monotonic() - started

    threads = [threading.Thread(target=order_a), threading.Thread(target=order_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results['a'] == 'ok'
    assert 'exceeds the remaining budget' in results['b']
    assert results['b_waited'] > 1.0             # B really waited for A's lock
    total = db.execute('select coalesce(sum(total), 0) from inventory.purchase_orders').fetchone()[0]
    assert total <= 20000


def test_conversation_survives_reconnection_without_model_costs(db, service):
    """A scripted model and the real PostgresSaver: close the connection, reopen it, the thread is still there."""
    from langchain_core.messages import AIMessage
    from inventory_agent import new_thread_id, open_agent, run_turn

    class Scripted:
        def __init__(self, *replies):
            self.replies = list(replies)

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages):
            return self.replies.pop(0)

    service.reset_scenario('disruption')
    thread = new_thread_id('disruption')
    config = {'configurable': {'thread_id': thread}}
    model = Scripted(AIMessage(content='', tool_calls=[{'name': 'list_inventory', 'args': {}, 'id': 'c1'}]),
                     AIMessage(content='Inventory read.'), AIMessage(content='Still here.'))
    with open_agent(service, model=model, db_uri=URL) as graph:
        run_turn(graph, thread, 'Read the inventory only.', emit=lambda _: None)
    with open_agent(service, model=model, db_uri=URL) as graph:          # a new connection
        assert len(graph.get_state(config).values['messages']) == 4
        run_turn(graph, thread, 'Are you still there?', emit=lambda _: None)
        assert len(graph.get_state(config).values['messages']) == 6


def test_checkpoint_version_mismatch_is_refused(db, service):
    db.execute('delete from inventory_checkpoints.checkpoint_migrations where v = 9')
    try:
        from inventory_agent import open_agent
        with pytest.raises(RuntimeError, match='Checkpoint schema is at version 8'):
            with open_agent(service, model=object(), db_uri=URL):
                pass
    finally:
        db.execute('insert into inventory_checkpoints.checkpoint_migrations (v) values (9)')


def test_anon_and_authenticated_cannot_call_the_functions(db):
    for role in ('anon', 'authenticated'):
        with connect() as conn, conn.transaction():
            conn.execute(f'set local role {role}')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute('select public.inventory_snapshot()')
