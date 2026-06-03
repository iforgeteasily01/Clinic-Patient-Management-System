-- Seed managementsys_patientstatus
-- Rows correspond 1-to-1 with the integer values stored in ActivePatient.status

INSERT INTO managementsys_patientstatus (id, status_name)
VALUES
    (0, 'Invalid'),
    (1, 'Waiting - Consultation'),
    (2, 'In Consultation'),
    (3, 'Waiting - Treatment'),
    (4, 'In Treatment'),
    (5, 'Waiting - Billing')
ON CONFLICT (id) DO UPDATE
    SET status_name = EXCLUDED.status_name;

-- Reset the auto-increment sequence so new rows don't collide with these seeded IDs
SELECT setval(
    pg_get_serial_sequence('managementsys_patientstatus', 'id'),
    (SELECT MAX(id) FROM managementsys_patientstatus)
);
