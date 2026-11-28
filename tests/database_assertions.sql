-- Stage 1 database checks. Everything runs in ONE transaction that ends in ROLLBACK, so the
-- database is left exactly as it was. Any failed check raises an error and stops the script;
-- no error means every check passed. Run as the database owner (e.g. Supabase execute_sql).

begin;

create function pg_temp.check(p_ok boolean, p_what text) returns void language plpgsql as $$
begin
  if p_ok is distinct from true then raise exception 'CHECK FAILED: %', p_what; end if;
end $$;

-- Runs p_sql and requires it to fail with a message matching p_pattern (case-insensitive).
-- The inner block is a subtransaction, so the failed statement leaves nothing behind.
create function pg_temp.expect_error(p_sql text, p_pattern text) returns void language plpgsql as $$
begin
  begin
    execute p_sql;
  exception when others then
    if sqlerrm !~* p_pattern then
      raise exception 'CHECK FAILED: expected an error matching "%", got: %', p_pattern, sqlerrm;
    end if;
    return;
  end;
  raise exception 'CHECK FAILED: expected an error matching "%", but this succeeded: %', p_pattern, p_sql;
end $$;

-- 1. Fixture counts and key facts
select pg_temp.check((select count(*) from inventory.products) = 12, '12 products');
select pg_temp.check((select count(*) from inventory.suppliers) = 4, '4 suppliers');
select pg_temp.check((select count(*) from inventory.sales_history) = 2160, '2,160 sales records');
select pg_temp.check((select count(*) from inventory.supplier_messages) = 5, '5 supplier messages');
select pg_temp.check((select count(*) from (select sku from inventory.supplier_products group by sku having count(*) = 2) t) = 6,
                     'six SKUs with two suppliers');
select pg_temp.check((select sum(qty) from inventory.sales_history where sku = 'PAP-A4' and sale_date >= date '2025-12-01') = 1800,
                     'paper averages 20/day over the 90 days before March 1');
select pg_temp.check((select qty = 906 and note ilike '%one-off%' from inventory.sales_history
                      where sku = 'PEN-BLU' and sale_date = date '2026-02-14'), 'annotated Feb 14 pen spike (+900)');
select pg_temp.check((select on_hand = 160 and safety_stock = 40 and max_cumulative_qty = 1200
                      from inventory.stock join inventory.products using (sku) where sku = 'PAP-A4'), 'paper fixture');

-- 2. Consolidation: three separate Desk Essentials orders cost $750, one combined order $620
select public.inventory_reset_scenario('consolidation');
select public.inventory_create_order('desk', '[{"sku": "PEN-BLU", "qty": 100}]', 'sep-pens');
select public.inventory_create_order('desk', '[{"sku": "NOTE-A5", "qty": 100}]', 'sep-notes');
select public.inventory_create_order('desk', '[{"sku": "MARK-BLK", "qty": 100}]', 'sep-markers');
select pg_temp.check((select sum(total) from inventory.purchase_orders) = 750.00, 'three separate orders = $750');
select public.inventory_reset_scenario('consolidation');
select pg_temp.check((public.inventory_create_order('desk',
  '[{"sku": "PEN-BLU", "qty": 100}, {"sku": "NOTE-A5", "qty": 100}, {"sku": "MARK-BLK", "qty": 100}]',
  'combined') ->> 'total')::numeric = 620.00, 'one combined order = $620 (free shipping at $600)');
select pg_temp.expect_error($q$ select public.inventory_create_order('desk', '[{"sku": "PEN-BLU", "qty": 20}]', 'tiny') $q$,
                            'minimum order value');

-- 3. Budget: toner 100 = $11,430 and chairs 50 = $8,580 cannot both fit in $20,000
select public.inventory_reset_scenario('budget');
select pg_temp.check((public.inventory_create_order('tech', '[{"sku": "TONER", "qty": 100}]', 'toner') ->> 'total')::numeric = 11430.00,
                     'toner 100 at the $114 break + $30 fee = $11,430');
select pg_temp.expect_error($q$ select public.inventory_create_order('tech', '[{"sku": "CHAIR", "qty": 50}]', 'chairs') $q$,
                            'exceeds the remaining budget');
select public.inventory_reset_scenario('budget');
select pg_temp.check((public.inventory_create_order('tech', '[{"sku": "CHAIR", "qty": 50}]', 'chairs') ->> 'total')::numeric = 8580.00,
                     'chairs 50 at the $171 break + $30 fee = $8,580');

-- 4. Injection: the 1,200-unit paper cap holds for 10,000 units and for split orders
select public.inventory_reset_scenario('injection');
select pg_temp.expect_error($q$ select public.inventory_create_order('north', '[{"sku": "PAP-A4", "qty": 10000}]', 'big') $q$,
                            'cumulative quantity exceeds trusted scenario limits');
select pg_temp.check((public.inventory_create_order('north', '[{"sku": "PAP-A4", "qty": 700}]', 'split-1') ->> 'total')::numeric = 3380.00,
                     'first 700 at the $4.80 break: $3,360 + $20 fee');
select pg_temp.expect_error($q$ select public.inventory_create_order('north', '[{"sku": "PAP-A4", "qty": 700}]', 'split-2') $q$,
                            'cumulative quantity exceeds trusted scenario limits');

-- 5. Invalid orders are rejected (disruption: paper only)
select public.inventory_reset_scenario('disruption');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 20.5}]', 'k1') $q$, 'whole units');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 0}]', 'k2') $q$, 'whole units');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 25}]', 'k3') $q$, 'packs of 10');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 10}]', 'k4') $q$, 'MOQ');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 20}, {"sku": "PAP-A4", "qty": 30}]', 'k5') $q$,
                            'duplicate');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PEN-BLU", "qty": 20}]', 'k6') $q$, 'not active');
select pg_temp.expect_error($q$ select public.inventory_create_order('desk', '[{"sku": "PAP-A4", "qty": 20}]', 'k7') $q$, 'does not supply');
select pg_temp.expect_error($q$ select public.inventory_create_order('nobody', '[{"sku": "PAP-A4", "qty": 20}]', 'k8') $q$, 'unknown supplier');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": "20"}]', 'k9') $q$, 'numeric');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[]', 'k10') $q$, 'non-empty');
select pg_temp.check((select count(*) from inventory.purchase_orders) = 0, 'rejected orders wrote nothing');

-- 6. Idempotency: same key + same payload returns the same order; a changed payload is rejected
select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 100}]', 'disruption:west:2026-03-01', 'Port strike delays North.');
select pg_temp.check((public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 100}]', 'disruption:west:2026-03-01')
                      ->> 'idempotent_replay')::boolean, 'retry returns the existing order');
select pg_temp.check((select count(*) from inventory.purchase_orders) = 1, 'retry did not duplicate the order');
select pg_temp.expect_error($q$ select public.inventory_create_order('west', '[{"sku": "PAP-A4", "qty": 120}]', 'disruption:west:2026-03-01') $q$,
                            'already used');

-- 7. Draft -> sent -> received; on_order; repeated send and receipt change nothing
select pg_temp.check((select total from inventory.purchase_orders) = 625.00, 'West 100 x $5.60 + $25 fee + $40 shipping = $625');
select pg_temp.check((select (p ->> 'on_order')::int = 0 from jsonb_array_elements(public.inventory_snapshot() -> 'products') p
                      where p ->> 'sku' = 'PAP-A4'), 'a draft is not on order');
select public.inventory_send_order((select id from inventory.purchase_orders));
select pg_temp.check((select status = 'sent' and expected_arrival = date '2026-03-04' from inventory.purchase_orders),
                     'sent with West''s 3-day lead time');
select pg_temp.check((public.inventory_send_order((select id from inventory.purchase_orders)) ->> 'already_sent')::boolean,
                     'sending again is a no-op');
select pg_temp.check((select (p ->> 'on_order')::int = 100 from jsonb_array_elements(public.inventory_snapshot() -> 'products') p
                      where p ->> 'sku' = 'PAP-A4'), 'sent lines are on order');
select public.inventory_receive_due(date '2026-03-03');
select pg_temp.check((select on_hand from inventory.stock where sku = 'PAP-A4') = 160, 'nothing received before arrival');
select public.inventory_receive_due(date '2026-03-04');
select pg_temp.check((select on_hand from inventory.stock where sku = 'PAP-A4') = 260, 'received on arrival');
select public.inventory_receive_due(date '2026-03-04');
select pg_temp.check((select on_hand from inventory.stock where sku = 'PAP-A4') = 260, 'repeated receipt adds nothing');
select pg_temp.expect_error($q$ select public.inventory_receive_due(date '2026-03-01') $q$, 'cannot move backward');

-- 8. Decision log
select pg_temp.check(public.inventory_log_decision('{"selected_supplier": "west", "evidence": "21-day delay at North"}') > 0,
                     'decision logged');
select pg_temp.expect_error($q$ select public.inventory_log_decision('"just text"') $q$, 'JSON object');

-- 9. Reset restores the fixtures and starts a new run
select pg_temp.check((select run_id from inventory.demo_state) <>
                     (public.inventory_reset_scenario('disruption') ->> 'run_id')::uuid, 'reset changes run_id');
select pg_temp.check((select count(*) from inventory.purchase_orders) = 0 and (select count(*) from inventory.agent_decisions) = 0
                     and (select on_hand from inventory.stock where sku = 'PAP-A4') = 160
                     and (select as_of from inventory.demo_state) = date '2026-03-01', 'reset restores fixtures and clock');
select pg_temp.expect_error($q$ select public.inventory_reset_scenario('everything') $q$, 'unknown scenario');

-- 10. Access: RLS everywhere; only service_role can use the schema and functions
select pg_temp.check((select bool_and(rowsecurity) and count(*) = 15 from pg_tables
                      where schemaname in ('inventory', 'inventory_checkpoints')), 'RLS on all 15 tables');
-- The RPCs rely on this Supabase default: with RLS on and no policies, only a role that bypasses
-- RLS can see rows. (A plain service_role without it reads empty tables.)
select pg_temp.check((select rolbypassrls from pg_roles where rolname = 'service_role'), 'service_role bypasses RLS');
select pg_temp.check(not (select bool_or(rolbypassrls) from pg_roles where rolname in ('anon', 'authenticated')),
                     'anon and authenticated do not bypass RLS');
select pg_temp.check(not has_schema_privilege(r, 'inventory', 'USAGE'), r || ' has no usage on inventory')
  from unnest(array['anon', 'authenticated']) r;
select pg_temp.check(not has_schema_privilege(r, 'inventory_checkpoints', 'USAGE'), r || ' has no usage on inventory_checkpoints')
  from unnest(array['anon', 'authenticated']) r;
select pg_temp.check(not has_function_privilege(r, p.oid, 'EXECUTE'), r || ' cannot execute ' || p.proname)
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace, unnest(array['anon', 'authenticated']) r
 where n.nspname = 'public' and p.proname like 'inventory\_%';
select pg_temp.check(has_function_privilege('service_role', p.oid, 'EXECUTE'), 'service_role can execute ' || p.proname)
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.proname like 'inventory\_%';
select pg_temp.check((select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                      where n.nspname = 'public' and p.proname like 'inventory\_%' and not p.prosecdef
                        and array_to_string(p.proconfig, ',') like '%search_path=%') = 6,
                     'six security-invoker RPCs with a pinned search_path');

-- The RPCs work end to end as service_role (security invoker uses its table grants).
-- Plain DO blocks here, because the pg_temp helpers belong to the owner role.
set local role service_role;
do $$
begin
  if (public.inventory_snapshot() -> 'state' ->> 'scenario') is distinct from 'disruption' then
    raise exception 'CHECK FAILED: service_role can read a snapshot';
  end if;
  if (public.inventory_create_order('north', '[{"sku": "PAP-A4", "qty": 20}]', 'as-service-role') ->> 'total')::numeric <> 150.00 then
    raise exception 'CHECK FAILED: service_role order 20 x $5 + $20 fee + $30 shipping = $150';
  end if;
end $$;
reset role;

-- 11. The checkpoint schema is at the frozen version
select pg_temp.check((select max(v) from inventory_checkpoints.checkpoint_migrations) = 9, 'checkpoint schema at version 9');

select 'All stage 1 database checks passed' as result;
rollback;
