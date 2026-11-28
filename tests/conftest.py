"""Offline fixtures: the agents-course snapshot, served by a fake Supabase client. No network, no key."""
import copy
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inventory_tools import InventoryService, Snapshot  # noqa: E402

FIXTURE = json.loads((ROOT / 'tests' / 'fixtures' / 'snapshot.json').read_text(encoding='utf-8'))

# Same formula and order as supabase/migrations/002_inventory_seed.sql.
BASES = {'PAP-A4': 20, 'PEN-BLU': 10, 'NOTE-A5': 6, 'MARK-BLK': 8, 'TONER': 3, 'CHAIR': 1,
         'STAPLER': 2, 'FOLDER': 15, 'ENV': 60, 'TAPE': 5, 'CLIP': 100, 'LABEL': 4}
WEEK = [Decimal('1.1'), Decimal('1.0'), Decimal('1.0'), Decimal('1.1'), Decimal('1.2'), Decimal('0.6'), Decimal('1.0')]
SPIKE_NOTE = 'One-off conference order: 900 extra boxes for the regional teachers conference. Not expected to recur.'


def generate_sales() -> list[dict]:
    rows, next_id = [], 1
    for sku in sorted(BASES):  # the database inserted ordered by sku, then date
        day = date(2025, 9, 2)
        while day <= date(2026, 2, 28):
            qty = int((BASES[sku] * WEEK[day.isoweekday() - 1]).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            spike = sku == 'PEN-BLU' and day == date(2026, 2, 14)
            rows.append({'id': next_id, 'sku': sku, 'sale_date': day.isoformat(),
                         'qty': qty + (900 if spike else 0), 'note': SPIKE_NOTE if spike else None})
            next_id += 1
            day += timedelta(days=1)
    return rows


def sales_md5(rows) -> str:
    """Same string the database hashed: id,sku,date,qty,note joined by ';' in id order."""
    text = ';'.join(f"{r['id']},{r['sku']},{r['sale_date']},{r['qty']},{r['note'] or ''}" for r in rows)
    return hashlib.md5(text.encode('utf-8')).hexdigest()


SALES = generate_sales()


def snapshot_data(scenario: str = 'disruption') -> dict:
    """The fixture snapshot, switched to a scenario the way inventory_reset_scenario does."""
    data = copy.deepcopy(FIXTURE['snapshot'])
    data['sales_history'] = copy.deepcopy(SALES)
    active = {'disruption': ['PAP-A4'], 'anomaly': ['PEN-BLU'], 'consolidation': ['MARK-BLK', 'NOTE-A5', 'PEN-BLU'],
              'budget': sorted(BASES), 'injection': ['PAP-A4']}[scenario]
    data['state'].update(scenario=scenario, active_skus=active)
    for p in data['products']:
        p['active'] = p['sku'] in active
    return data


class FakeClient:
    """Serves a snapshot for reads and records every write RPC (returning a canned reply)."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, copy.deepcopy(params)))
        reply = copy.deepcopy(self.data) if name == 'inventory_snapshot' else {'ok': True, 'name': name}
        class Response:
            data = reply
        class Builder:
            def execute(self):
                return Response()
        return Builder()


@pytest.fixture
def make_service():
    def build(scenario='disruption', orders=None):
        data = snapshot_data(scenario)
        if orders:
            data['orders'] = orders
        return InventoryService(FakeClient(data))
    return build


@pytest.fixture
def snap():
    def build(scenario='disruption', orders=None):
        data = snapshot_data(scenario)
        if orders:
            data['orders'] = orders
        return Snapshot(data)
    return build
