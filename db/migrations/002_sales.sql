-- =====================================================================
-- Этап 2: продажи клиентам, скидки, долги, оплаты, сдача денег, сводка.
-- Применяется командой: python -m tools.migrate (для каждого клиента).
-- =====================================================================

-- ---------- Номера заявок: «№12» вместо «З-00012» ----------
create or replace function create_request(p_agent uuid, p_kind text, p_items jsonb) returns jsonb
language plpgsql as $$
declare
  v_req requests; v_wh uuid; v_limit numeric; v_car numeric; v_add numeric; it jsonb;
begin
  if not exists (select 1 from users where id = p_agent and role = 'agent' and active) then
    raise exception 'NOT_AGENT';
  end if;
  if p_kind not in ('issue','return') then raise exception 'BAD_KIND'; end if;
  if p_items is null or jsonb_array_length(p_items) = 0 then raise exception 'EMPTY'; end if;
  v_wh := agent_wh(p_agent);

  if p_kind = 'issue' then
    v_limit := coalesce(setting_num('agent_stock_limit'), 0);
    select coalesce(sum(s.qty * p.price), 0) into v_car
      from stock s join products p on p.id = s.product_id where s.warehouse_id = v_wh;
    select coalesce(sum((i.v ->> 'qty')::int * p.price), 0) into v_add
      from jsonb_array_elements(p_items) as i(v) join products p on p.id = (i.v ->> 'product_id')::uuid;
    if v_limit > 0 and v_car + v_add > v_limit then
      raise exception 'LIMIT_EXCEEDED:%:%', round(v_car), round(v_limit);
    end if;
  else
    for it in select value from jsonb_array_elements(p_items) loop
      if coalesce((select qty from stock where warehouse_id = v_wh and product_id = (it ->> 'product_id')::uuid), 0)
         < (it ->> 'qty')::int then
        raise exception 'RETURN_MORE_THAN_CAR';
      end if;
    end loop;
  end if;

  insert into requests(agent_id, kind, no)
  values (p_agent, p_kind, '№' || nextval('request_no_seq'))
  returning * into v_req;

  insert into request_items(request_id, product_id, qty_requested, qty_actual)
  select v_req.id, (i.v ->> 'product_id')::uuid, (i.v ->> 'qty')::int, (i.v ->> 'qty')::int
    from jsonb_array_elements(p_items) as i(v)
   where (i.v ->> 'qty')::int > 0;

  insert into audit_log(user_id, action, details)
  values (p_agent, 'request_created', jsonb_build_object('no', v_req.no, 'kind', p_kind));
  return jsonb_build_object('id', v_req.id, 'no', v_req.no, 'kind', p_kind);
end $$;

-- ---------- Таблицы ----------
create sequence sale_no_seq start 1;

create table sales (
  id          uuid primary key default gen_random_uuid(),
  no          integer unique not null default nextval('sale_no_seq'),   -- номер накладной
  agent_id    uuid not null references users,
  client_id   uuid not null references clients,
  base_total  numeric(14,2) not null,          -- сумма по прайсу
  total       numeric(14,2) not null,          -- к оплате со скидками
  pay_type    text not null check (pay_type in ('cash','transfer','debt','part')),
  paid_now    numeric(14,2) not null default 0,
  status      text not null default 'done' check (status in ('done','pending','rejected')),
  created_at  timestamptz not null default now(),
  decided_by  uuid references users,           -- кто одобрил/отклонил скидку
  decided_at  timestamptz
);

create table sale_items (
  id           uuid primary key default gen_random_uuid(),
  sale_id      uuid not null references sales on delete cascade,
  product_id   uuid not null references products,
  qty          integer not null check (qty > 0),
  price        numeric(12,2) not null,         -- цена по прайсу на момент продажи
  discount_pct numeric(5,2) not null default 0 check (discount_pct between 0 and 100),
  amount       numeric(14,2) not null          -- сумма строки со скидкой
);

-- Долг по каждой продаже отдельно: так видно, какой долг старше
create table debts (
  id         uuid primary key default gen_random_uuid(),
  client_id  uuid not null references clients,
  sale_id    uuid references sales,
  amount     numeric(14,2) not null check (amount > 0),
  remaining  numeric(14,2) not null check (remaining >= 0),
  created_at timestamptz not null default now()
);
create index debts_open on debts (client_id, created_at) where remaining > 0;

create sequence handover_no_seq start 1;
create table handovers (
  id         uuid primary key default gen_random_uuid(),
  no         integer unique not null default nextval('handover_no_seq'),
  agent_id   uuid not null references users,
  amount     numeric(14,2) not null default 0 check (amount >= 0),
  status     text not null default 'new' check (status in ('new','confirmed','rejected')),
  created_at timestamptz not null default now(),
  decided_by uuid references users,
  decided_at timestamptz
);

-- Деньги от клиентов. Наличные у агента «на руках», пока не сданы (handover_id пустой).
create table payments (
  id          uuid primary key default gen_random_uuid(),
  client_id   uuid not null references clients,
  agent_id    uuid references users,           -- пусто = деньги приняты сразу в кассу
  sale_id     uuid references sales,
  amount      numeric(14,2) not null check (amount > 0),
  method      text not null check (method in ('cash','transfer')),
  created_at  timestamptz not null default now(),
  handover_id uuid references handovers
);
create index payments_on_hand on payments (agent_id) where method = 'cash' and handover_id is null;

-- ---------- Проверка кредита клиента ----------
create or replace function check_credit(p_client uuid, p_new_debt numeric) returns void
language plpgsql as $$
declare
  v_stop numeric := coalesce(setting_num('debt_stop_days'), 0);
  v_limit numeric := coalesce(setting_num('client_debt_limit'), 0);
  v_debt numeric; v_days numeric;
begin
  if p_new_debt is null or p_new_debt <= 0 then return; end if;
  select coalesce(sum(remaining), 0), coalesce(max(extract(epoch from now() - created_at) / 86400), 0)
    into v_debt, v_days
    from debts where client_id = p_client and remaining > 0;
  if v_stop > 0 and v_days > v_stop then raise exception 'DEBT_STOP:%', floor(v_days); end if;
  if v_limit > 0 and v_debt + p_new_debt > v_limit then
    raise exception 'DEBT_LIMIT:%:%', round(v_debt), round(v_limit);
  end if;
end $$;

-- ---------- Проведение продажи: товар из машины, оплата, долг ----------
create or replace function execute_sale(p_sale uuid) returns void
language plpgsql as $$
declare s sales; it record; v_wh uuid; v_debt numeric;
begin
  select * into s from sales where id = p_sale for update;
  v_wh := agent_wh(s.agent_id);
  for it in select * from sale_items where sale_id = s.id loop
    perform move_stock(it.product_id, it.qty, v_wh, null, 'sale', s.id, '№' || s.no, s.agent_id);
  end loop;
  if s.paid_now > 0 then
    insert into payments(client_id, agent_id, sale_id, amount, method)
    values (s.client_id, s.agent_id, s.id, s.paid_now, case when s.pay_type = 'transfer' then 'transfer' else 'cash' end);
  end if;
  v_debt := s.total - s.paid_now;
  if v_debt > 0 then
    insert into debts(client_id, sale_id, amount, remaining) values (s.client_id, s.id, v_debt, v_debt);
  end if;
  update sales set status = 'done' where id = s.id;
end $$;

-- ---------- Агент оформляет продажу ----------
-- p_items = [{"product_id": "...", "qty": 10, "discount_pct": 5}], p_pay = cash|transfer|debt|part
create or replace function create_sale(p_agent uuid, p_client uuid, p_items jsonb, p_pay text, p_paid numeric)
returns jsonb language plpgsql as $$
declare
  v_sale sales; v_wh uuid; it record; v_base numeric := 0; v_total numeric := 0;
  v_need boolean := false; v_paid numeric;
begin
  if not exists (select 1 from users where id = p_agent and role = 'agent' and active) then raise exception 'NOT_AGENT'; end if;
  if not exists (select 1 from clients where id = p_client and agent_id = p_agent) then raise exception 'NOT_YOUR_CLIENT'; end if;
  if p_pay not in ('cash','transfer','debt','part') then raise exception 'BAD_PAY'; end if;
  if p_items is null or jsonb_array_length(p_items) = 0 then raise exception 'EMPTY'; end if;
  v_wh := agent_wh(p_agent);

  for it in
    select (i.v ->> 'qty')::int as qty,
           least(greatest(coalesce((i.v ->> 'discount_pct')::numeric, 0), 0), 100) as pct,
           p.price, coalesce(g.max_discount, 0) as maxd, coalesce(s.qty, 0) as car_qty
      from jsonb_array_elements(p_items) as i(v)
      join products p on p.id = (i.v ->> 'product_id')::uuid
      left join product_groups g on g.id = p.group_id
      left join stock s on s.warehouse_id = v_wh and s.product_id = p.id
  loop
    if it.qty is null or it.qty <= 0 then raise exception 'BAD_QTY'; end if;
    if it.qty > it.car_qty then raise exception 'NOT_IN_CAR'; end if;
    v_base := v_base + it.price * it.qty;
    v_total := v_total + round(it.price * it.qty * (1 - it.pct / 100), 2);
    if it.pct > it.maxd then v_need := true; end if;   -- скидка выше лимита группы → к владельцу
  end loop;

  v_paid := case p_pay when 'cash' then v_total when 'transfer' then v_total when 'debt' then 0
                       else least(greatest(coalesce(p_paid, 0), 0), v_total) end;
  perform check_credit(p_client, v_total - v_paid);

  insert into sales(agent_id, client_id, base_total, total, pay_type, paid_now, status)
  values (p_agent, p_client, v_base, v_total, p_pay, v_paid, case when v_need then 'pending' else 'done' end)
  returning * into v_sale;

  insert into sale_items(sale_id, product_id, qty, price, discount_pct, amount)
  select v_sale.id, p.id, x.qty, p.price, x.pct, round(p.price * x.qty * (1 - x.pct / 100), 2)
    from (select (i.v ->> 'product_id')::uuid as pid, (i.v ->> 'qty')::int as qty,
                 least(greatest(coalesce((i.v ->> 'discount_pct')::numeric, 0), 0), 100) as pct
            from jsonb_array_elements(p_items) as i(v)) x
    join products p on p.id = x.pid;

  if not v_need then perform execute_sale(v_sale.id); end if;
  insert into audit_log(user_id, action, details)
  values (p_agent, 'sale_created', jsonb_build_object('no', v_sale.no, 'total', v_total, 'pending', v_need));
  return jsonb_build_object('id', v_sale.id, 'no', v_sale.no, 'status', v_sale.status,
                            'total', v_total, 'base_total', v_base, 'paid', v_paid);
end $$;

-- ---------- Владелец одобряет или отклоняет скидку ----------
create or replace function decide_sale(p_sale uuid, p_user uuid, p_approve boolean) returns jsonb
language plpgsql as $$
declare s sales;
begin
  if not exists (select 1 from users where id = p_user and role = 'owner' and active) then raise exception 'FORBIDDEN'; end if;
  select * into s from sales where id = p_sale for update;
  if not found then raise exception 'NOT_FOUND'; end if;
  if s.status <> 'pending' then raise exception 'ALREADY_DONE'; end if;
  if p_approve then
    perform check_credit(s.client_id, s.total - s.paid_now);
    perform execute_sale(s.id);
  else
    update sales set status = 'rejected' where id = s.id;
  end if;
  update sales set decided_by = p_user, decided_at = now() where id = s.id;
  insert into audit_log(user_id, action, details)
  values (p_user, 'sale_decided', jsonb_build_object('no', s.no, 'approved', p_approve));
  return jsonb_build_object('id', s.id, 'no', s.no, 'agent_id', s.agent_id, 'approved', p_approve);
end $$;

-- ---------- Оплата долга: гасит самые старые долги первыми ----------
create or replace function take_payment(p_user uuid, p_client uuid, p_amount numeric, p_method text) returns jsonb
language plpgsql as $$
declare u users; c clients; d record; v_got numeric; v_left numeric; v_part numeric;
begin
  select * into u from users where id = p_user and active;
  if not found or u.role not in ('agent','owner','accountant') then raise exception 'FORBIDDEN'; end if;
  select * into c from clients where id = p_client;
  if not found then raise exception 'NOT_FOUND'; end if;
  if u.role = 'agent' and c.agent_id is distinct from u.id then raise exception 'NOT_YOUR_CLIENT'; end if;
  if p_method not in ('cash','transfer') then raise exception 'BAD_PAY'; end if;

  select coalesce(sum(remaining), 0) into v_got from debts where client_id = p_client and remaining > 0;
  v_got := least(v_got, round(coalesce(p_amount, 0), 2));
  if v_got <= 0 then raise exception 'NO_DEBT'; end if;

  v_left := v_got;
  for d in select * from debts where client_id = p_client and remaining > 0 order by created_at for update loop
    exit when v_left <= 0;
    v_part := least(v_left, d.remaining);
    update debts set remaining = remaining - v_part where id = d.id;
    v_left := v_left - v_part;
  end loop;

  insert into payments(client_id, agent_id, amount, method)
  values (p_client, case when u.role = 'agent' then u.id end, v_got, p_method);
  insert into audit_log(user_id, action, details)
  values (p_user, 'payment', jsonb_build_object('client', c.name, 'amount', v_got, 'method', p_method));
  return jsonb_build_object('paid', v_got,
         'debt_left', (select coalesce(sum(remaining), 0) from debts where client_id = p_client));
end $$;

-- ---------- Агент сдаёт наличные в кассу ----------
create or replace function create_handover(p_agent uuid) returns jsonb
language plpgsql as $$
declare h handovers; v_sum numeric;
begin
  if not exists (select 1 from users where id = p_agent and role = 'agent' and active) then raise exception 'NOT_AGENT'; end if;
  insert into handovers(agent_id) values (p_agent) returning * into h;
  with upd as (
    update payments set handover_id = h.id
     where agent_id = p_agent and method = 'cash' and handover_id is null
    returning amount)
  select coalesce(sum(amount), 0) into v_sum from upd;
  if v_sum <= 0 then raise exception 'NO_CASH'; end if;
  update handovers set amount = v_sum where id = h.id;
  return jsonb_build_object('id', h.id, 'no', h.no, 'amount', v_sum);
end $$;

-- Бухгалтер подтверждает получение денег (или отклоняет — деньги снова числятся за агентом)
create or replace function decide_handover(p_id uuid, p_user uuid, p_ok boolean) returns jsonb
language plpgsql as $$
declare h handovers;
begin
  if not exists (select 1 from users where id = p_user and role in ('owner','accountant') and active) then
    raise exception 'FORBIDDEN';
  end if;
  select * into h from handovers where id = p_id for update;
  if not found then raise exception 'NOT_FOUND'; end if;
  if h.status <> 'new' then raise exception 'ALREADY_DONE'; end if;
  if p_ok then
    update handovers set status = 'confirmed', decided_by = p_user, decided_at = now() where id = h.id;
  else
    update handovers set status = 'rejected', decided_by = p_user, decided_at = now() where id = h.id;
    update payments set handover_id = null where handover_id = h.id;
  end if;
  insert into audit_log(user_id, action, details)
  values (p_user, 'handover_decided', jsonb_build_object('no', h.no, 'amount', h.amount, 'ok', p_ok));
  return jsonb_build_object('id', h.id, 'no', h.no, 'agent_id', h.agent_id, 'amount', h.amount, 'ok', p_ok);
end $$;

-- ---------- Представления ----------
create view v_client_debts as
select c.id as client_id, c.agent_id,
       coalesce(sum(d.remaining), 0) as debt,
       coalesce(floor(max(extract(epoch from now() - d.created_at) / 86400)), 0)::int as oldest_days
  from clients c
  left join debts d on d.client_id = c.id and d.remaining > 0
 group by c.id, c.agent_id;

create view v_agent_cash as
select u.id as agent_id, u.full_name, u.active,
       coalesce((select sum(amount) from payments p
                  where p.agent_id = u.id and p.method = 'cash' and p.handover_id is null), 0) as cash_on_hand,
       coalesce((select sum(amount) from handovers h where h.agent_id = u.id and h.status = 'new'), 0) as pending_handover
  from users u where u.role = 'agent';

create or replace function today_start() returns timestamptz
language sql stable as $$ select date_trunc('day', now() at time zone 'Asia/Bishkek') at time zone 'Asia/Bishkek' $$;

-- Сводка для владельца одним вызовом
create or replace function owner_summary() returns jsonb
language sql stable as $$
select jsonb_build_object(
  'sales_today',     (select coalesce(sum(total), 0) from sales where status = 'done' and created_at >= today_start()),
  'sales_count',     (select count(*) from sales where status = 'done' and created_at >= today_start()),
  'discount_today',  (select coalesce(sum(base_total - total), 0) from sales where status = 'done' and created_at >= today_start()),
  'debts_total',     (select coalesce(sum(debt), 0) from v_client_debts),
  'debts_overdue',   (select coalesce(sum(debt), 0) from v_client_debts
                       where oldest_days > coalesce(setting_num('debt_stop_days'), 0) and coalesce(setting_num('debt_stop_days'), 0) > 0),
  'goods_at_agents', (select coalesce(sum(value), 0) from v_stock where kind = 'agent'),
  'cash_at_agents',  (select coalesce(sum(cash_on_hand + pending_handover), 0) from v_agent_cash),
  'pending_approvals', (select count(*) from sales where status = 'pending'),
  'pending_handovers', (select count(*) from handovers where status = 'new'),
  'agents', (select coalesce(jsonb_agg(a order by a ->> 'name'), '[]'::jsonb) from (
      select jsonb_build_object(
        'name', u.full_name,
        'sales_today', (select coalesce(sum(total), 0) from sales s where s.agent_id = u.id and s.status = 'done' and s.created_at >= today_start()),
        'car', (select coalesce(sum(value), 0) from v_stock v where v.agent_id = u.id),
        'cash', (select cash_on_hand + pending_handover from v_agent_cash c where c.agent_id = u.id),
        'debts', (select coalesce(sum(debt), 0) from v_client_debts d where d.agent_id = u.id)) as a
        from users u where u.role = 'agent' and u.active) t)
) $$;
