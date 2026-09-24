-- Gainesville listings tracker — initial schema bootstrap.
-- All later changes go through numbered migrations under migrations/.

CREATE TABLE IF NOT EXISTS _schema_migrations (
    migration_id  TEXT PRIMARY KEY,
    applied_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS property_register (
    property_id              TEXT PRIMARY KEY,
    parcel_id                TEXT,
    name                     TEXT,
    address                  TEXT,
    city                     TEXT,
    zip                      TEXT,
    in_city_limits           BOOLEAN,
    lat                      DOUBLE,
    lon                      DOUBLE,
    property_type            TEXT,
    units                    INTEGER,
    year_built               INTEGER,
    owner_name_raw           TEXT,
    owner_entity_normalized  TEXT,
    first_seen               TIMESTAMP,
    last_updated             TIMESTAMP,
    source_of_truth          TEXT,
    notes                    TEXT
);

CREATE TABLE IF NOT EXISTS raw_observations (
    observation_id      TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    source              TEXT NOT NULL,
    source_listing_id   TEXT,
    url                 TEXT,
    scraped_at          TIMESTAMP NOT NULL,
    listing_kind        TEXT,
    address_raw         TEXT,
    address_normalized  TEXT,
    addr_norm_version   INTEGER,
    city                TEXT,
    zip                 TEXT,
    lat                 DOUBLE,
    lon                 DOUBLE,
    beds                DOUBLE,
    baths               DOUBLE,
    sqft                INTEGER,
    asking_rent         INTEGER,
    concessions_text    TEXT,
    date_posted         DATE,
    date_available      DATE,
    title               TEXT,
    body                TEXT,
    raw_payload_path    TEXT,
    processed           BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_raw_obs_run ON raw_observations(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_obs_addr ON raw_observations(address_normalized, beds);

CREATE TABLE IF NOT EXISTS canonical_listings (
    canonical_id        TEXT PRIMARY KEY,
    property_id         TEXT REFERENCES property_register(property_id),
    listing_kind        TEXT,
    address_normalized  TEXT,
    beds                DOUBLE,
    baths               DOUBLE,
    sqft                INTEGER,
    first_seen          DATE,
    last_seen           DATE,
    days_on_market      INTEGER,
    status              TEXT,
    current_rent        INTEGER,
    min_rent_observed   INTEGER,
    max_rent_observed   INTEGER,
    sources_seen        TEXT[],
    observation_ids     TEXT[],
    merge_confidence    DOUBLE,
    manual_override     BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_canon_addr ON canonical_listings(address_normalized, beds);

CREATE TABLE IF NOT EXISTS run_log (
    run_id           TEXT PRIMARY KEY,
    started_at       TIMESTAMP,
    finished_at      TIMESTAMP,
    status           TEXT,
    sources_ok       TEXT[],
    sources_failed   TEXT[],
    new_observations INTEGER,
    new_canonicals   INTEGER,
    review_queue     INTEGER,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS dedup_review (
    review_id           TEXT PRIMARY KEY,
    candidate_a_obs_id  TEXT,
    candidate_b_obs_id  TEXT,
    proposed_canonical  TEXT,
    confidence          DOUBLE,
    reason              TEXT,
    decided             BOOLEAN DEFAULT FALSE,
    decision            TEXT,
    decided_at          TIMESTAMP
);

CREATE TABLE IF NOT EXISTS zori_history (
    zip          TEXT,
    month        DATE,
    zori_value   DOUBLE,
    home_type    TEXT,
    fetched_at   TIMESTAMP,
    PRIMARY KEY (zip, month, home_type)
);
