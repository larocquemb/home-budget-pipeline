-- Public daily FX observations are separate from immutable bank transactions.
CREATE TABLE IF NOT EXISTS accounting.exchange_rate_downloads (
 id BIGSERIAL PRIMARY KEY,
 provider TEXT NOT NULL,
 source_url TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 payload JSONB NOT NULL,
 actor TEXT NOT NULL,
 fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS accounting.exchange_rate_observations (
 download_id BIGINT NOT NULL REFERENCES accounting.exchange_rate_downloads(id),
 rate_date DATE NOT NULL,
 rate NUMERIC(18,10) NOT NULL CHECK(rate>0),
 PRIMARY KEY(download_id,rate_date)
);
CREATE TABLE IF NOT EXISTS accounting.exchange_rates (
 provider TEXT NOT NULL,
 base_currency CHAR(3) NOT NULL,
 quote_currency CHAR(3) NOT NULL,
 rate_date DATE NOT NULL,
 rate NUMERIC(18,10) NOT NULL CHECK(rate>0),
 download_id BIGINT NOT NULL REFERENCES accounting.exchange_rate_downloads(id),
 PRIMARY KEY(provider,base_currency,quote_currency,rate_date)
);
CREATE OR REPLACE VIEW accounting.transaction_ledger_cad AS
SELECT t.id AS transaction_id,t.account_id,t.transaction_date AS bank_date,
 COALESCE(a.nickname,a.account_reference,t.source_account_reference) AS account,
 COALESCE(a.primary_card_last4,(SELECT n.last4 FROM accounting.account_numbers n
   WHERE n.account_id=a.id AND n.number_kind='bank_account' ORDER BY n.id LIMIT 1)) AS account_last4,
 t.description,t.amount AS original_amount,t.currency,t.card_last4,
 CASE WHEN t.currency='CAD' THEN 1::numeric ELSE r.rate END AS fx_rate,
 CASE WHEN t.currency='CAD' THEN t.transaction_date ELSE r.rate_date END AS rate_date,
 CASE WHEN t.currency='CAD' THEN t.amount
      WHEN t.currency='USD' AND r.rate IS NOT NULL THEN round(t.amount*r.rate,2) END AS amount_cad,
 CASE WHEN t.currency='CAD' THEN 'native_cad'
      WHEN t.currency<>'USD' THEN 'unsupported_currency'
      WHEN r.rate IS NULL THEN 'missing_rate'
      WHEN r.rate_date=t.transaction_date THEN 'boc_daily'
      ELSE 'boc_prior_business_day' END AS conversion_status
FROM accounting.financial_transactions t
LEFT JOIN accounting.accounts a ON a.id=t.account_id
LEFT JOIN LATERAL (
 SELECT rate,rate_date FROM accounting.exchange_rates
 WHERE provider='bank_of_canada' AND base_currency='USD' AND quote_currency='CAD'
   AND t.currency='USD' AND rate_date<=t.transaction_date AND rate_date>=t.transaction_date-7
 ORDER BY rate_date DESC LIMIT 1
) r ON true;
