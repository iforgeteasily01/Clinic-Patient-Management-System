-- migrate_tbl_supel_to_patient.sql
--
-- Migrates patient records from ipos.tbl_supel (tipe='PL') into
-- the Django managementsys_patient table.
--
-- Run this against the target Django database (cpms):
--   psql -U postgres -d cpms -f migrate_tbl_supel_to_patient.sql
--
-- Rows skipped (not inserted):
--   - kode        > 10 chars  (15 rows  — merged/decommissioned records)
--   - nama        > 100 chars (0 rows)
--   - alamat      > 100 chars (1 row)
--   - telepon     > 15 chars  (42 rows)
-- Expected inserts: ~16,238 out of 16,296 PL rows.
-- ----------------------------------------------------------------

-- Enable dblink if not already enabled
CREATE EXTENSION IF NOT EXISTS dblink;

INSERT INTO managementsys_patient (patient_no, name, address, phone_number)
SELECT
    kode,
    COALESCE(nama, ''),
    COALESCE(alamat, ''),
    COALESCE(telepon, '')
FROM dblink(
    'host=localhost dbname=ipos user=postgres password=seesaw',
    $q$
        SELECT
            kode,
            nama,
            alamat,
            telepon
        FROM tbl_supel
        WHERE tipe = 'PL'
          AND LENGTH(kode)                  <= 10
          AND LENGTH(COALESCE(nama, ''))    <= 100
          AND LENGTH(COALESCE(alamat, ''))  <= 100
          AND LENGTH(COALESCE(telepon, '')) <= 15
    $q$
) AS src(
    kode        VARCHAR(10),
    nama        VARCHAR(100),
    alamat      VARCHAR(100),
    telepon     VARCHAR(15)
)
ON CONFLICT (patient_no) DO NOTHING;
