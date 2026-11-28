-- Stage 1: schemas and tables for the inventory-agent exercise.
-- Integer units, numeric money. RLS on every table with no policies: only the
-- service role (which bypasses RLS) and the database owner can reach the data.

create schema inventory;
create schema inventory_checkpoints;
revoke all on schema inventory, inventory_checkpoints from public, anon, authenticated;
grant usage on schema inventory to service_role;

create table inventory.suppliers (
  id text primary key,
  name text not null unique,
  lead_time_days integer not null check (lead_time_days > 0),         -- master lead time
  ordering_fee numeric(12,2) not null check (ordering_fee >= 0),      -- per order
  shipping_fee numeric(12,2) not null check (shipping_fee >= 0),      -- charged below the threshold
  free_shipping_threshold numeric(12,2) not null check (free_shipping_threshold >= 0),
  min_order_value numeric(12,2) not null default 0 check (min_order_value >= 0)
);

create table inventory.products (
  sku text primary key,
  name text not null,
  unit text not null,
  holding_rate numeric(5,4) not null check (holding_rate > 0 and holding_rate <= 1),  -- annual, share of price
  safety_stock integer not null check (safety_stock >= 0),
  max_cumulative_qty integer not null check (max_cumulative_qty > 0)  -- trusted cap per scenario run
);

create table inventory.supplier_products (
  supplier_id text not null references inventory.suppliers (id),
  sku text not null references inventory.products (sku),
  unit_price numeric(12,2) not null check (unit_price > 0),
  moq integer not null check (moq > 0),
  pack_size integer not null check (pack_size > 0),
  primary key (supplier_id, sku),
  check (moq % pack_size = 0)  -- the minimum must be orderable in whole packs
);
create index supplier_products_sku_idx on inventory.supplier_products (sku);

-- All-unit discounts: at or above min_qty, every unit costs unit_price.
create table inventory.price_breaks (
  supplier_id text not null,
  sku text not null,
  min_qty integer not null check (min_qty > 0),
  unit_price numeric(12,2) not null check (unit_price > 0),
  primary key (supplier_id, sku, min_qty),
  foreign key (supplier_id, sku) references inventory.supplier_products (supplier_id, sku)
);

create table inventory.stock (
  sku text primary key references inventory.products (sku),
  on_hand integer not null check (on_hand >= 0),
  initial_on_hand integer not null check (initial_on_hand >= 0)  -- restored by the reset function
);

create table inventory.sales_history (
  id bigint generated always as identity primary key,
  sku text not null references inventory.products (sku),
  sale_date date not null,
  qty integer not null check (qty >= 0),
  note text,  -- annotation; untrusted free text
  unique (sku, sale_date)
);

create table inventory.supplier_messages (
  id integer primary key,
  supplier_id text not null references inventory.suppliers (id),
  scenario text not null check (scenario in ('disruption', 'anomaly', 'consolidation', 'budget', 'injection')),
  received_at timestamptz not null,
  body text not null  -- untrusted free text
);
create index supplier_messages_supplier_idx on inventory.supplier_messages (supplier_id);

-- One row: the simulated clock and the current scenario run.
create table inventory.demo_state (
  id integer primary key default 1 check (id = 1),
  scenario text not null check (scenario in ('disruption', 'anomaly', 'consolidation', 'budget', 'injection')),
  as_of date not null,
  deadline date not null,
  run_id uuid not null,
  active_skus text[] not null,
  budget numeric(12,2) not null check (budget >= 0)
);

create table inventory.purchase_orders (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null,
  request_key text not null check (length(btrim(request_key)) > 0),
  supplier_id text not null references inventory.suppliers (id),
  status text not null default 'draft' check (status in ('draft', 'sent', 'received')),
  payload jsonb not null,  -- canonical request, compared on idempotent retries
  merchandise numeric(12,2) not null check (merchandise >= 0),
  ordering_fee numeric(12,2) not null check (ordering_fee >= 0),
  shipping_fee numeric(12,2) not null check (shipping_fee >= 0),
  total numeric(12,2) not null check (total >= 0),
  rationale text not null default '',
  created_on date not null,
  sent_on date,
  expected_arrival date,
  received_on date,
  unique (run_id, request_key)
);
create index purchase_orders_supplier_idx on inventory.purchase_orders (supplier_id);

create table inventory.po_lines (
  po_id uuid not null references inventory.purchase_orders (id) on delete cascade,
  sku text not null references inventory.products (sku),
  qty integer not null check (qty > 0),
  unit_price numeric(12,2) not null check (unit_price >= 0),
  line_total numeric(12,2) not null check (line_total >= 0),
  primary key (po_id, sku)
);
create index po_lines_sku_idx on inventory.po_lines (sku);

create table inventory.agent_decisions (
  id bigint generated always as identity primary key,
  run_id uuid not null,
  as_of date not null,
  decision jsonb not null check (jsonb_typeof(decision) = 'object'),
  created_at timestamptz not null default now()
);

alter table inventory.suppliers enable row level security;
alter table inventory.products enable row level security;
alter table inventory.supplier_products enable row level security;
alter table inventory.price_breaks enable row level security;
alter table inventory.stock enable row level security;
alter table inventory.sales_history enable row level security;
alter table inventory.supplier_messages enable row level security;
alter table inventory.demo_state enable row level security;
alter table inventory.purchase_orders enable row level security;
alter table inventory.po_lines enable row level security;
alter table inventory.agent_decisions enable row level security;

revoke all on all tables in schema inventory from public, anon, authenticated;
grant select, insert, update, delete on all tables in schema inventory to service_role;
grant usage, select on all sequences in schema inventory to service_role;
