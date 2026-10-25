-- =====================================================================
-- ERKai Склад — схема базы данных (один проект Supabase = один клиент)
-- Запуск: Supabase → SQL Editor → вставить весь файл → Run.
-- Повторный запуск на той же базе не предусмотрен: для обновлений
-- будут отдельные файлы миграций.
-- =====================================================================

create extension if not exists pgcrypto;

-- ---------- Настройки компании ----------
-- Значения хранятся как JSON: число, true/false или текст.
create table settings (
  key   text primary key,
  value jsonb not null
);
insert into settings(key, value) values
  ('agent_stock_limit',          '300000'),  -- сколько товара (в сомах) может быть у агента на руках; 0 = без лимита
  ('client_debt_limit',          '60000'),   -- лимит долга на одного клиента (этап 2)
  ('debt_stop_days',             '30'),      -- через сколько дней долг блокирует продажу в долг (этап 2)
  ('notify_accountant_on_moves', 'true');    -- присылать бухгалтеру каждое движение товара

-- ---------- Сотрудники ----------
create table users (
  id         uuid primary key default gen_random_uuid(),
  tg_id      bigint unique not null,           -- Telegram ID сотрудника
  full_name  text not null,
  role       text not null check (role in ('owner','accountant','storekeeper','agent')),
  active     boolean not null default true,    -- false = доступ к боту отключён
  created_at timestamptz not null default now()
);

-- Приглашения: владелец или бухгалтер создаёт код, сотрудник открывает ссылку в боте.
create table invites (
  code       text primary key,
  role       text not null check (role in ('accountant','storekeeper','agent')),
  full_name  text not null,
  created_by uuid references users,
  created_at timestamptz not null default now(),
  used_by    uuid references users,
  used_at    timestamptz
);

-- ---------- Склады ----------
-- Один основной склад и по одному «складу-машине» на каждого агента.
create table warehouses (
  id       uuid primary key default gen_random_uuid(),
  name     text not null,
  kind     text not null check (kind in ('main','agent')),
  agent_id uuid unique references users
);
create unique index warehouses_one_main on warehouses ((kind)) where kind = 'main';
insert into warehouses(name, kind) values ('Основной склад', 'main');

-- ---------- Товары ----------
create table product_groups (
  id           uuid primary key default gen_random_uuid(),
  name         text unique not null,
  max_discount numeric(5,2) not null default 0   -- допустимая скидка агента в %, этап 2
);

create table products (
  id         uuid primary key default gen_random_uuid(),
  code_1c    text unique,                         -- код номенклатуры в 1С
  name       text not null,
  group_id   uuid references product_groups,
  unit       text not null default 'шт',
  price      numeric(12,2) not null default 0,    -- базовая цена из прайса 1С
  active     boolean not null default true,
  updated_at timestamptz not null default now()
);

-- Текущие остатки по каждому складу. Количество только целое (дробных нет).
create table stock (
  warehouse_id uuid not null references warehouses on delete cascade,
  product_id   uuid not null references products on delete cascade,
  qty          integer not null default 0 check (qty >= 0),
  primary key (warehouse_id, product_id)
);

-- ---------- Клиенты (принадлежат компании) ----------
create table clients (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  place      text,                     -- рынок, ряд, адрес точки
  phone      text,
  agent_id   uuid references users,    -- кто сейчас обслуживает; можно передать другому
  code_1c    text unique,              -- код контрагента в 1С
  note       text,
  created_by uuid references users,
  created_at timestamptz not null default now()
);

-- ---------- Заявки агентов (выдача и возврат) ----------
create sequence request_no_seq start 1;

create table requests (
  id           uuid primary key default gen_random_uuid(),
  no           text unique not null,      -- З-00001 (выдача) или В-00002 (возврат)
  agent_id     uuid not null references users,
  kind         text not null check (kind in ('issue','return')),
  status       text not null default 'new' check (status in ('new','done','cancelled')),
  comment      text,
  created_at   timestamptz not null default now(),
  confirmed_by uuid references users,
  confirmed_at timestamptz
);

create table request_items (
  id            uuid primary key default gen_random_uuid(),
  request_id    uuid not null references requests on delete cascade,
  product_id    uuid not null references products,
  qty_requested integer not null check (qty_requested > 0),
  qty_actual    integer                    -- сколько фактически выдал / принял складовщик
);

-- ---------- Журнал движений (только добавление) ----------
-- Каждая строка — одно перемещение товара. Отсюда формируется выгрузка в 1С.
create table movements (
  id          bigserial primary key,
  ts          timestamptz not null default now(),
  product_id  uuid not null references products,
  qty         integer not null check (qty > 0),
  from_wh     uuid references warehouses,   -- null = приход извне (например, корректировка из 1С)
  to_wh       uuid references warehouses,   -- null = уход наружу
  doc_type    text not null,                -- issue / return / import_1c
  doc_id      uuid,
  doc_no      text,
  user_id     uuid references users,
  exported_1c boolean not null default false
);
create index movements_ts on movements (ts desc);

create table audit_log (
  id      bigserial primary key,
  ts      timestamptz not null default now(),
  user_id uuid references users,
  action  text not null,
  details jsonb
);

-- =====================================================================
-- Функции
-- =====================================================================

create or replace function main_wh() returns uuid
language sql stable as $$ select id from warehouses where kind = 'main' limit 1 $$;

create or replace function agent_wh(p_agent uuid) returns uuid
language sql stable as $$ select id from warehouses where agent_id = p_agent $$;

create or replace function setting_num(p_key text) returns numeric
language sql stable as $$ select (value #>> '{}')::numeric from settings where key = p_key $$;

-- Каждому новому агенту автоматически создаётся его «склад-машина».
create or replace function trg_agent_warehouse() returns trigger
language plpgsql as $$
begin
  if new.role = 'agent' and not exists (select 1 from warehouses where agent_id = new.id) then
    insert into warehouses(name, kind, agent_id) values ('Машина: ' || new.full_name, 'agent', new.id);
  end if;
  return new;
end $$;
create trigger users_agent_warehouse after insert or update of role on users
  for each row execute function trg_agent_warehouse();

-- Перемещение товара между складами + запись в журнал. Основа всех операций.
create or replace function move_stock(
  p_product uuid, p_qty integer, p_from uuid, p_to uuid,
  p_doc_type text, p_doc_id uuid, p_doc_no text, p_user uuid
) returns void
language plpgsql as $$
begin
  if p_qty is null or p_qty <= 0 then return; end if;
  if p_from is not null then
    update stock set qty = qty - p_qty
     where warehouse_id = p_from and product_id = p_product and qty >= p_qty;
    if not found then raise exception 'NOT_ENOUGH_STOCK'; end if;
  end if;
  if p_to is not null then
    insert into stock(warehouse_id, product_id, qty) values (p_to, p_product, p_qty)
    on conflict (warehouse_id, product_id) do update set qty = stock.qty + excluded.qty;
  end if;
  insert into movements(product_id, qty, from_wh, to_wh, doc_type, doc_id, doc_no, user_id)
  values (p_product, p_qty, p_from, p_to, p_doc_type, p_doc_id, p_doc_no, p_user);
end $$;

-- Регистрация сотрудника по коду приглашения.
create or replace function use_invite(p_code text, p_tg bigint) returns jsonb
language plpgsql as $$
declare inv invites; u users;
begin
  select * into inv from invites where code = upper(trim(p_code)) and used_by is null for update;
  if not found then raise exception 'BAD_INVITE'; end if;
  insert into users(tg_id, full_name, role) values (p_tg, inv.full_name, inv.role)
  on conflict (tg_id) do update set role = excluded.role, full_name = excluded.full_name, active = true
  returning * into u;
  update invites set used_by = u.id, used_at = now() where code = inv.code;
  insert into audit_log(user_id, action, details) values (u.id, 'invite_used', jsonb_build_object('role', u.role));
  return to_jsonb(u);
end $$;

-- Агент создаёт заявку. p_items = [{"product_id": "...", "qty": 10}, ...]
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
    -- Лимит подотчёта: товар в машине + новая заявка не больше лимита
    v_limit := coalesce(setting_num('agent_stock_limit'), 0);
    select coalesce(sum(s.qty * p.price), 0) into v_car
      from stock s join products p on p.id = s.product_id where s.warehouse_id = v_wh;
    select coalesce(sum((i.v ->> 'qty')::int * p.price), 0) into v_add
      from jsonb_array_elements(p_items) as i(v) join products p on p.id = (i.v ->> 'product_id')::uuid;
    if v_limit > 0 and v_car + v_add > v_limit then
      raise exception 'LIMIT_EXCEEDED:%:%', round(v_car), round(v_limit);
    end if;
  else
    -- Вернуть можно только то, что есть в машине
    for it in select value from jsonb_array_elements(p_items) loop
      if coalesce((select qty from stock where warehouse_id = v_wh and product_id = (it ->> 'product_id')::uuid), 0)
         < (it ->> 'qty')::int then
        raise exception 'RETURN_MORE_THAN_CAR';
      end if;
    end loop;
  end if;

  insert into requests(agent_id, kind, no)
  values (p_agent, p_kind,
          case when p_kind = 'issue' then 'З-' else 'В-' end || lpad(nextval('request_no_seq')::text, 5, '0'))
  returning * into v_req;

  insert into request_items(request_id, product_id, qty_requested, qty_actual)
  select v_req.id, (i.v ->> 'product_id')::uuid, (i.v ->> 'qty')::int, (i.v ->> 'qty')::int
    from jsonb_array_elements(p_items) as i(v)
   where (i.v ->> 'qty')::int > 0;

  insert into audit_log(user_id, action, details)
  values (p_agent, 'request_created', jsonb_build_object('no', v_req.no, 'kind', p_kind));
  return jsonb_build_object('id', v_req.id, 'no', v_req.no, 'kind', p_kind);
end $$;

-- Складовщик подтверждает выдачу или возврат.
-- p_items = [{"item_id": "...", "qty_actual": 80}] — только изменённые строки; null = как в заявке.
create or replace function confirm_request(p_request uuid, p_user uuid, p_items jsonb) returns jsonb
language plpgsql as $$
declare
  r requests; it record; v_main uuid := main_wh(); v_wh uuid; v_qty int; v_role text; v_short int := 0;
begin
  select role into v_role from users where id = p_user and active;
  if v_role is null or v_role not in ('storekeeper','owner','accountant') then raise exception 'FORBIDDEN'; end if;

  select * into r from requests where id = p_request for update;
  if not found then raise exception 'NOT_FOUND'; end if;
  if r.status <> 'new' then raise exception 'ALREADY_DONE'; end if;
  v_wh := agent_wh(r.agent_id);

  for it in select * from request_items where request_id = r.id loop
    v_qty := coalesce(
      (select (x.v ->> 'qty_actual')::int from jsonb_array_elements(coalesce(p_items, '[]'::jsonb)) as x(v)
        where (x.v ->> 'item_id')::uuid = it.id limit 1),
      it.qty_requested);
    v_qty := greatest(0, v_qty);
    if r.kind = 'issue' then
      v_qty := least(v_qty, coalesce((select qty from stock where warehouse_id = v_main and product_id = it.product_id), 0));
      perform move_stock(it.product_id, v_qty, v_main, v_wh, 'issue', r.id, r.no, p_user);
    else
      v_qty := least(v_qty, coalesce((select qty from stock where warehouse_id = v_wh and product_id = it.product_id), 0));
      perform move_stock(it.product_id, v_qty, v_wh, v_main, 'return', r.id, r.no, p_user);
    end if;
    if v_qty < it.qty_requested then v_short := v_short + 1; end if;
    update request_items set qty_actual = v_qty where id = it.id;
  end loop;

  update requests set status = 'done', confirmed_by = p_user, confirmed_at = now() where id = r.id;
  insert into audit_log(user_id, action, details)
  values (p_user, 'request_confirmed', jsonb_build_object('no', r.no, 'short_items', v_short));
  return jsonb_build_object('id', r.id, 'no', r.no, 'kind', r.kind, 'agent_id', r.agent_id, 'short', v_short);
end $$;

-- Отмена заявки (агентом, пока складовщик не подтвердил).
create or replace function cancel_request(p_request uuid, p_user uuid) returns void
language plpgsql as $$
begin
  update requests set status = 'cancelled'
   where id = p_request and status = 'new'
     and (agent_id = p_user or exists (select 1 from users where id = p_user and role in ('owner','storekeeper','accountant')));
  if not found then raise exception 'CANNOT_CANCEL'; end if;
end $$;

-- Передача клиентов от одного агента другому (например, при увольнении).
create or replace function transfer_clients(p_from uuid, p_to uuid, p_by uuid, p_block boolean) returns integer
language plpgsql as $$
declare n int; v_role text;
begin
  select role into v_role from users where id = p_by and active;
  if v_role is null or v_role not in ('owner','accountant') then raise exception 'FORBIDDEN'; end if;
  if p_from = p_to then raise exception 'SAME_AGENT'; end if;
  if not exists (select 1 from users where id = p_to and role = 'agent' and active) then
    raise exception 'TARGET_NOT_AGENT';
  end if;
  update clients set agent_id = p_to where agent_id = p_from;
  get diagnostics n = row_count;
  if p_block then update users set active = false where id = p_from; end if;
  insert into audit_log(user_id, action, details)
  values (p_by, 'clients_transferred', jsonb_build_object('from', p_from, 'to', p_to, 'count', n, 'blocked', p_block));
  return n;
end $$;

-- Установить остаток основного склада (по данным 1С) через корректирующее движение.
create or replace function set_main_stock(p_product uuid, p_qty integer) returns void
language plpgsql as $$
declare cur int; v_main uuid := main_wh();
begin
  select qty into cur from stock where warehouse_id = v_main and product_id = p_product;
  cur := coalesce(cur, 0);
  if p_qty > cur then
    perform move_stock(p_product, p_qty - cur, null, v_main, 'import_1c', null, '1С', null);
  elsif p_qty < cur then
    perform move_stock(p_product, cur - p_qty, v_main, null, 'import_1c', null, '1С', null);
  end if;
end $$;

-- Загрузка номенклатуры из 1С одним вызовом.
-- p_rows = [{"code": "000123", "name": "...", "group": "...", "unit": "меш.", "price": 420, "stock": 320}]
create or replace function import_catalog(p_rows jsonb) returns jsonb
language plpgsql as $$
declare r jsonb; g uuid; pid uuid; n int := 0;
begin
  for r in select value from jsonb_array_elements(p_rows) loop
    g := null;
    if coalesce(r ->> 'group', '') <> '' then
      insert into product_groups(name) values (r ->> 'group') on conflict (name) do nothing;
      select id into g from product_groups where name = r ->> 'group';
    end if;
    insert into products(code_1c, name, group_id, unit, price, updated_at)
    values (r ->> 'code', r ->> 'name', g, coalesce(nullif(r ->> 'unit', ''), 'шт'),
            coalesce((r ->> 'price')::numeric, 0), now())
    on conflict (code_1c) do update
      set name = excluded.name, group_id = excluded.group_id, unit = excluded.unit,
          price = excluded.price, active = true, updated_at = now()
    returning id into pid;
    if r ? 'stock' and r ->> 'stock' is not null then
      perform set_main_stock(pid, round((r ->> 'stock')::numeric)::int);
    end if;
    n := n + 1;
  end loop;
  return jsonb_build_object('rows', n);
end $$;

-- =====================================================================
-- Представления (готовые выборки для бота)
-- =====================================================================

create view v_catalog as
select p.id, p.code_1c, p.name, p.unit, p.price,
       g.name as group_name, coalesce(g.max_discount, 0) as max_discount,
       coalesce(s.qty, 0) as stock
  from products p
  left join product_groups g on g.id = p.group_id
  left join stock s on s.product_id = p.id and s.warehouse_id = main_wh()
 where p.active;

create view v_stock as
select w.id as warehouse_id, w.kind, w.agent_id, w.name as warehouse,
       p.id as product_id, p.name as product, p.unit, p.price, s.qty, s.qty * p.price as value
  from stock s
  join warehouses w on w.id = s.warehouse_id
  join products p on p.id = s.product_id
 where s.qty > 0;

create view v_movements as
select m.id, m.ts, m.doc_type, m.doc_no, p.name as product, p.unit, m.qty,
       wf.name as from_name, wt.name as to_name, u.full_name as user_name, m.exported_1c
  from movements m
  join products p on p.id = m.product_id
  left join warehouses wf on wf.id = m.from_wh
  left join warehouses wt on wt.id = m.to_wh
  left join users u on u.id = m.user_id;

-- =====================================================================
-- Безопасность: доступ к данным только с сервера бота (service_role).
-- Публичный ключ anon не видит ни таблиц, ни функций.
-- =====================================================================
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on all tables    in schema public from anon, authenticated;
    revoke all on all sequences in schema public from anon, authenticated;
    revoke execute on all functions in schema public from anon, authenticated;
  end if;
end $$;
revoke execute on all functions in schema public from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on all tables    in schema public to service_role;
    grant all on all sequences in schema public to service_role;
    grant execute on all functions in schema public to service_role;
  end if;
end $$;
