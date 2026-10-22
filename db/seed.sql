-- Demo data. Safe to re-run.

-- Demo user. Token is "demo-token-change-me" (sha256 below) -- change it before any public demo:
--   select encode(digest('<new token>', 'sha256'), 'hex');
insert into app_user (id, display_name, base_currency, api_token_hash)
values ('00000000-0000-0000-0000-000000000001', 'Can', 'EUR',
        encode(digest('demo-token-change-me', 'sha256'), 'hex'))
on conflict (id) do nothing;

-- Fixed 20-stock demo universe.
insert into universe_symbol (symbol, name) values
    ('AAPL', 'Apple'),            ('MSFT', 'Microsoft'),
    ('NVDA', 'NVIDIA'),           ('AMZN', 'Amazon'),
    ('GOOGL', 'Alphabet'),        ('META', 'Meta Platforms'),
    ('TSLA', 'Tesla'),            ('AVGO', 'Broadcom'),
    ('JPM', 'JPMorgan Chase'),    ('V', 'Visa'),
    ('MA', 'Mastercard'),         ('UNH', 'UnitedHealth'),
    ('XOM', 'Exxon Mobil'),       ('JNJ', 'Johnson & Johnson'),
    ('PG', 'Procter & Gamble'),   ('HD', 'Home Depot'),
    ('COST', 'Costco'),           ('NFLX', 'Netflix'),
    ('AMD', 'Advanced Micro Devices'), ('KO', 'Coca-Cola')
on conflict (symbol) do nothing;

-- USD anchor so conversions work before the first FX refresh.
insert into fx_rate (currency, usd_per_unit, as_of, source)
values ('USD', 1, now(), 'fixed')
on conflict (currency) do nothing;

-- ── Default dashboard (the state the demo always starts from) ─────────────────────
-- Fixed ids, so re-running is a no-op. Prices and FX come from the valuation refresh.
-- Back to exactly this state at any time: db/reset_demo.sh
insert into asset (id, user_id, kind, name, amount, currency, color) values
    ('a371cb29-a9f5-47b3-b29b-bef8d26ad464', '00000000-0000-0000-0000-000000000001', 'cash', 'Revolut', 7500, 'EUR', '#7da8c8'),
    ('4a784f7f-820f-411d-a854-84b156a9f032', '00000000-0000-0000-0000-000000000001', 'cash', 'BNP Paribas', 12000, 'EUR', '#52c48a'),
    ('9b462de7-e020-4b52-b44a-feb301cc7e47', '00000000-0000-0000-0000-000000000001', 'cash', 'Garanti BBVA', 500000, 'TRY', '#c49a52')
on conflict (id) do nothing;

insert into liability (id, user_id, counterparty, amount, currency, note) values
    ('3d26c7be-7507-474f-b8b5-10778fab7689', '00000000-0000-0000-0000-000000000001', 'Alex', 1500, 'EUR', null),
    ('97024631-de84-43a6-a586-789f13f3496f', '00000000-0000-0000-0000-000000000001', 'BNP Paribas', 5000, 'EUR', 'Loan'),
    ('921a47d9-3b00-45a1-b623-148558854c7e', '00000000-0000-0000-0000-000000000001', 'Société Générale', 40000, 'EUR', 'Loan')
on conflict (id) do nothing;

-- 500 shares each, bought 20-40% below the price on 2026-10-16.
insert into holding (id, user_id, symbol, name, quantity, cost_per_share, cost_currency) values
    ('d55720f9-2c94-4fed-9311-e45dee3c2313', '00000000-0000-0000-0000-000000000001', 'NVDA', 'NVIDIA Corporation', 500, 155.22, 'USD'),
    ('c64a17e1-8ac4-44b2-b124-274181551766', '00000000-0000-0000-0000-000000000001', 'NET', 'Cloudflare, Inc.', 500, 223.16, 'USD'),
    ('2ef6a3ac-9145-43f9-8eec-34983e5b6b8d', '00000000-0000-0000-0000-000000000001', 'NBIS', 'Nebius Group N.V.', 500, 112.77, 'USD'),
    ('ff4d6966-1b70-4abd-aaa2-12b345d217ad', '00000000-0000-0000-0000-000000000001', 'DDOG', 'Datadog, Inc.', 500, 173.16, 'USD'),
    ('1f4357a6-1270-429d-b31f-791a36666555', '00000000-0000-0000-0000-000000000001', 'SNOW', 'Snowflake Inc.', 500, 229.2, 'USD')
on conflict (id) do nothing;

insert into watchlist (id, user_id, symbol, name, position) values
    ('ff763b06-853a-4e44-8b72-eeb47563f59a', '00000000-0000-0000-0000-000000000001', 'NVDA', 'NVIDIA Corporation', 1),
    ('3430f2af-d345-40a1-b593-c737d544b564', '00000000-0000-0000-0000-000000000001', 'NET', 'Cloudflare, Inc.', 2),
    ('fea81eb6-0e90-4e3d-aa7c-02056839faea', '00000000-0000-0000-0000-000000000001', 'NBIS', 'Nebius Group N.V.', 3),
    ('bbf54f42-1717-440b-824c-d1a7fb3b85e9', '00000000-0000-0000-0000-000000000001', 'DDOG', 'Datadog, Inc.', 4),
    ('c0472f1d-4bdf-4638-aa7c-2491c226cfe9', '00000000-0000-0000-0000-000000000001', 'SNOW', 'Snowflake Inc.', 5)
on conflict (id) do nothing;
