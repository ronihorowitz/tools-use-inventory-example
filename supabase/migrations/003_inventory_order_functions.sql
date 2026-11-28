-- Stage 1: the narrow RPC surface. Every function is SECURITY INVOKER with a pinned, empty
-- search_path, and only service_role may execute it. Prices, discounts, fees, caps and the
-- budget are computed here from trusted tables; nothing about price is taken from the caller.
-- Every DELETE/UPDATE has an explicit WHERE: Supabase API sessions enable safeupdate.
-- Reset and clock advancement are separate functions so future model tools can exclude them.

-- Private helper: one order with its lines, as JSON.
create function inventory.order_json(p_id uuid) returns jsonb
language sql stable security invoker set search_path = ''
as $$
  select (to_jsonb(o) - 'payload') || jsonb_build_object('lines',
           coalesce((select jsonb_agg(to_jsonb(l) - 'po_id' order by l.sku)
                     from inventory.po_lines l where l.po_id = o.id), '[]'::jsonb))
  from inventory.purchase_orders o
  where o.id = p_id;
$$;

-- Read-only view of everything the tools need, for the current scenario run.
create function public.inventory_snapshot() returns jsonb
language sql stable security invoker set search_path = ''
as $$
  with st as (select * from inventory.demo_state where id = 1),
  on_order as (
    select l.sku, sum(l.qty)::int as qty
    from inventory.po_lines l
    join inventory.purchase_orders o on o.id = l.po_id
    join st on o.run_id = st.run_id
    where o.status = 'sent'
    group by l.sku
  )
  select jsonb_build_object(
    'state', (select to_jsonb(st) from st),
    'suppliers', (select jsonb_agg(to_jsonb(s) order by s.id) from inventory.suppliers s),
    'products', (select jsonb_agg(to_jsonb(p) || jsonb_build_object(
                          'on_hand', k.on_hand, 'on_order', coalesce(oo.qty, 0),
                          'active', p.sku = any (st.active_skus)) order by p.sku)
                 from inventory.products p
                 join inventory.stock k on k.sku = p.sku
                 left join on_order oo on oo.sku = p.sku
                 cross join st),
    'supplier_products', (select jsonb_agg(to_jsonb(sp) order by sp.sku, sp.supplier_id) from inventory.supplier_products sp),
    'price_breaks', (select jsonb_agg(to_jsonb(pb) order by pb.sku, pb.supplier_id, pb.min_qty) from inventory.price_breaks pb),
    'sales_history', (select jsonb_agg(to_jsonb(h) order by h.sku, h.sale_date) from inventory.sales_history h),
    'supplier_messages', (select jsonb_agg(to_jsonb(m) order by m.id) from inventory.supplier_messages m),
    'orders', coalesce((select jsonb_agg(inventory.order_json(o.id) order by o.created_on, o.request_key)
                        from inventory.purchase_orders o join st on o.run_id = st.run_id), '[]'::jsonb),
    'decisions', coalesce((select jsonb_agg(to_jsonb(d) order by d.id)
                           from inventory.agent_decisions d join st on d.run_id = st.run_id), '[]'::jsonb)
  );
$$;

-- Notebook utility, never a model tool: restore this demo's fixtures and start a new run.
create function public.inventory_reset_scenario(p_scenario text) returns jsonb
language plpgsql security invoker set search_path = ''
as $$
declare
  v_active text[];
begin
  v_active := case p_scenario
    when 'disruption'    then array['PAP-A4']
    when 'anomaly'       then array['PEN-BLU']
    when 'consolidation' then array['MARK-BLK', 'NOTE-A5', 'PEN-BLU']
    when 'budget'        then (select array_agg(sku order by sku) from inventory.products)
    when 'injection'     then array['PAP-A4']
  end;
  if v_active is null then
    raise exception 'Unknown scenario %; use disruption, anomaly, consolidation, budget or injection', p_scenario;
  end if;
  perform 1 from inventory.demo_state where id = 1 for update;
  delete from inventory.purchase_orders where id is not null;  -- lines cascade
  delete from inventory.agent_decisions where id is not null;
  update inventory.stock set on_hand = initial_on_hand where sku is not null;
  update inventory.demo_state
     set scenario = p_scenario, as_of = date '2026-03-01', deadline = date '2026-03-15',
         run_id = gen_random_uuid(), active_skus = v_active, budget = 20000.00
   where id = 1;
  return (select to_jsonb(s) from inventory.demo_state s where id = 1);
end;
$$;

-- Create a draft order. Locks the scenario state, so concurrent calls are serialized and
-- cannot overspend. Retrying with the same request_key and the same payload returns the
-- existing order; the same key with a different payload is rejected.
create function public.inventory_create_order(p_supplier text, p_lines jsonb, p_request_key text,
                                              p_rationale text default '') returns jsonb
language plpgsql security invoker set search_path = ''
as $$
declare
  st inventory.demo_state%rowtype;
  sup inventory.suppliers%rowtype;
  sp inventory.supplier_products%rowtype;
  existing inventory.purchase_orders%rowtype;
  r record;
  v_qty numeric;
  v_payload jsonb;
  v_priced jsonb := '[]'::jsonb;
  v_price numeric(12,2);
  v_used integer;
  v_cap integer;
  v_merch numeric(12,2) := 0;
  v_ship numeric(12,2);
  v_total numeric(12,2);
  v_committed numeric(12,2);
  v_id uuid;
begin
  select * into st from inventory.demo_state where id = 1 for update;

  if p_request_key is null or btrim(p_request_key) = '' then
    raise exception 'request_key is required';
  end if;
  select * into sup from inventory.suppliers where id = p_supplier;
  if not found then
    raise exception 'Unknown supplier %', p_supplier;
  end if;
  if p_lines is null or jsonb_typeof(p_lines) <> 'array' or jsonb_array_length(p_lines) = 0 then
    raise exception 'lines must be a non-empty array of {"sku": ..., "qty": ...}';
  end if;
  for r in select e from jsonb_array_elements(p_lines) as e loop
    if jsonb_typeof(r.e) <> 'object' or jsonb_typeof(r.e -> 'sku') is distinct from 'string'
       or jsonb_typeof(r.e -> 'qty') is distinct from 'number' then
      raise exception 'Each line needs a text "sku" and a numeric "qty": %', r.e;
    end if;
    v_qty := (r.e ->> 'qty')::numeric;
    if v_qty <= 0 or v_qty <> trunc(v_qty) or v_qty > 2147483647 then
      raise exception 'Quantities must be positive whole units; got % for %', v_qty, r.e ->> 'sku';
    end if;
  end loop;
  if (select count(*) <> count(distinct e ->> 'sku') from jsonb_array_elements(p_lines) as e) then
    raise exception 'Duplicate SKU lines are not allowed; combine them into one line';
  end if;

  select jsonb_build_object('supplier_id', p_supplier, 'lines',
           jsonb_agg(jsonb_build_object('sku', e ->> 'sku', 'qty', (e ->> 'qty')::numeric::int) order by e ->> 'sku'))
    into v_payload
    from jsonb_array_elements(p_lines) as e;

  select * into existing from inventory.purchase_orders where run_id = st.run_id and request_key = p_request_key;
  if found then
    if existing.payload = v_payload then
      return inventory.order_json(existing.id) || jsonb_build_object('idempotent_replay', true);
    end if;
    raise exception 'request_key % was already used for a different order; inspect the existing order or reset the scenario', p_request_key;
  end if;

  for r in select e ->> 'sku' as sku, (e ->> 'qty')::numeric::int as qty
           from jsonb_array_elements(p_lines) as e order by 1 loop
    if not (r.sku = any (st.active_skus)) then
      raise exception '% is not active in the % scenario', r.sku, st.scenario;
    end if;
    select * into sp from inventory.supplier_products where supplier_id = p_supplier and sku = r.sku;
    if not found then
      raise exception '% does not supply %', sup.name, r.sku;
    end if;
    if r.qty < sp.moq then
      raise exception '% needs at least % units from % (MOQ); got %', r.sku, sp.moq, sup.name, r.qty;
    end if;
    if r.qty % sp.pack_size <> 0 then
      raise exception '% is sold in packs of %; % is not a whole number of packs', r.sku, sp.pack_size, r.qty;
    end if;
    select coalesce((select pb.unit_price from inventory.price_breaks pb
                     where pb.supplier_id = p_supplier and pb.sku = r.sku and pb.min_qty <= r.qty
                     order by pb.min_qty desc limit 1), sp.unit_price)
      into v_price;
    select coalesce(sum(l.qty), 0) into v_used
      from inventory.po_lines l join inventory.purchase_orders o on o.id = l.po_id
     where o.run_id = st.run_id and l.sku = r.sku;
    select max_cumulative_qty into v_cap from inventory.products where sku = r.sku;
    if v_used + r.qty > v_cap then
      raise exception 'SKU or cumulative quantity exceeds trusted scenario limits: % would reach % units against a cap of % for this run (already ordered: %)',
        r.sku, v_used + r.qty, v_cap, v_used;
    end if;
    v_merch := v_merch + r.qty * v_price;
    v_priced := v_priced || jsonb_build_object('sku', r.sku, 'qty', r.qty, 'unit_price', v_price, 'line_total', r.qty * v_price);
  end loop;

  if v_merch < sup.min_order_value then
    raise exception 'Merchandise of % is below the % minimum order value of %', v_merch, sup.name, sup.min_order_value;
  end if;
  v_ship := case when v_merch < sup.free_shipping_threshold then sup.shipping_fee else 0 end;
  v_total := v_merch + sup.ordering_fee + v_ship;
  select coalesce(sum(total), 0) into v_committed from inventory.purchase_orders where run_id = st.run_id;
  if v_committed + v_total > st.budget then
    raise exception 'Order total % exceeds the remaining budget of % (budget %, already committed %)',
      v_total, st.budget - v_committed, st.budget, v_committed;
  end if;

  insert into inventory.purchase_orders (run_id, request_key, supplier_id, status, payload, merchandise,
                                         ordering_fee, shipping_fee, total, rationale, created_on)
  values (st.run_id, p_request_key, p_supplier, 'draft', v_payload, v_merch,
          sup.ordering_fee, v_ship, v_total, coalesce(p_rationale, ''), st.as_of)
  returning id into v_id;
  insert into inventory.po_lines (po_id, sku, qty, unit_price, line_total)
  select v_id, x ->> 'sku', (x ->> 'qty')::int, (x ->> 'unit_price')::numeric, (x ->> 'line_total')::numeric
    from jsonb_array_elements(v_priced) as x;
  return inventory.order_json(v_id);
end;
$$;

-- Mark a draft as sent (simulated; nothing is transmitted). Sending again changes nothing.
create function public.inventory_send_order(p_order uuid) returns jsonb
language plpgsql security invoker set search_path = ''
as $$
declare
  st inventory.demo_state%rowtype;
  o inventory.purchase_orders%rowtype;
begin
  select * into st from inventory.demo_state where id = 1 for update;
  select * into o from inventory.purchase_orders where id = p_order and run_id = st.run_id for update;
  if not found then
    raise exception 'Order % is not part of the current scenario run', p_order;
  end if;
  if o.status = 'draft' then
    update inventory.purchase_orders
       set status = 'sent', sent_on = st.as_of,
           expected_arrival = st.as_of + (select lead_time_days from inventory.suppliers where id = o.supplier_id)
     where id = p_order;
  end if;
  return inventory.order_json(p_order) || jsonb_build_object('already_sent', o.status <> 'draft');
end;
$$;

-- Notebook utility, never a model tool: move the simulated clock forward, then receive every
-- sent order whose expected arrival has come. Repeating the same date changes nothing.
create function public.inventory_receive_due(p_new_date date) returns jsonb
language plpgsql security invoker set search_path = ''
as $$
declare
  st inventory.demo_state%rowtype;
  v_due uuid[];
begin
  select * into st from inventory.demo_state where id = 1 for update;
  if p_new_date is null or p_new_date < st.as_of then
    raise exception 'The simulated clock cannot move backward: % is before %', p_new_date, st.as_of;
  end if;
  update inventory.demo_state set as_of = p_new_date where id = 1;

  select coalesce(array_agg(id), '{}') into v_due
    from inventory.purchase_orders
   where run_id = st.run_id and status = 'sent' and expected_arrival <= p_new_date;
  update inventory.stock k
     set on_hand = k.on_hand + x.qty
    from (select l.sku, sum(l.qty)::int as qty from inventory.po_lines l
          where l.po_id = any (v_due) group by l.sku) as x
   where k.sku = x.sku;
  update inventory.purchase_orders
     set status = 'received', received_on = expected_arrival
   where id = any (v_due);

  return jsonb_build_object('as_of', p_new_date,
    'received', coalesce((select jsonb_agg(inventory.order_json(id) order by id) from unnest(v_due) as id), '[]'::jsonb));
end;
$$;

-- Audit log of the agent's reasoning (evidence, exclusions, supplier choice, costs).
create function public.inventory_log_decision(p_decision jsonb) returns bigint
language plpgsql security invoker set search_path = ''
as $$
declare
  st inventory.demo_state%rowtype;
  v_id bigint;
begin
  if p_decision is null or jsonb_typeof(p_decision) <> 'object' then
    raise exception 'A decision must be a JSON object';
  end if;
  select * into st from inventory.demo_state where id = 1;
  insert into inventory.agent_decisions (run_id, as_of, decision)
  values (st.run_id, st.as_of, p_decision)
  returning id into v_id;
  return v_id;
end;
$$;

revoke all on function inventory.order_json(uuid) from public, anon, authenticated;
revoke all on function public.inventory_snapshot() from public, anon, authenticated;
revoke all on function public.inventory_reset_scenario(text) from public, anon, authenticated;
revoke all on function public.inventory_create_order(text, jsonb, text, text) from public, anon, authenticated;
revoke all on function public.inventory_send_order(uuid) from public, anon, authenticated;
revoke all on function public.inventory_receive_due(date) from public, anon, authenticated;
revoke all on function public.inventory_log_decision(jsonb) from public, anon, authenticated;

grant execute on function inventory.order_json(uuid) to service_role;
grant execute on function public.inventory_snapshot() to service_role;
grant execute on function public.inventory_reset_scenario(text) to service_role;
grant execute on function public.inventory_create_order(text, jsonb, text, text) to service_role;
grant execute on function public.inventory_send_order(uuid) to service_role;
grant execute on function public.inventory_receive_due(date) to service_role;
grant execute on function public.inventory_log_decision(jsonb) to service_role;
