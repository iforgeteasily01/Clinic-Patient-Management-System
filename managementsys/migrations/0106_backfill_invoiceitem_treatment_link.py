"""Link InvoiceItem rows that named a treatment but never pointed at it.

WHY THESE ROWS EXIST
--------------------
Not an import artefact, and not a one-off. ``POSPage.tsx`` builds invoice lines
from a Treatment in two places — ``handleTransferBilling`` (pull a patient off
the billing queue into the POS) and ``commitRedemptionDrafts`` (redeem a package
session) — and both set::

    { item_id: null, item_name: t.name, treatment_id: t.id }

The treatment id was sent all along; only ``item_id`` was dropped. Against the
2026-07-31 snapshot, 767 of 73.690 InvoiceItem rows (1,04%) carry no ``item_id``:

    2024-03 … 2024-07   94 rows   iPos history import, mostly 'Daily Skin Sunscreen'
    2026-06            170 rows   POSPage billing-transfer path
    2026-07            503 rows   same path, and growing

658 of the 767 name a Treatment exactly, and **every one of those Treatments has
a ``catalog_item``** — so the link was available at write time in 100% of cases.

WHY IT MATTERS
--------------
An unlinked line has no FK for ``journal_engine._line_revenue_account`` to route
by, so revenue falls back to matching the free-text name — which works until
somebody transposes two letters (migration 0077, 'Inejksi Flek 500'). It also
consumes no ``TreatmentMaterial``, so the treatment appears to cost nothing at
all. In June 2026 that was Rp 82.515.000 of service revenue reaching the ledger
only by name lookup.

The live bug is fixed in ``invoice_page.resolve_line_item_ids``, which links
these lines on the way in for every client, including Medya-Cashier. This
migration repairs the rows already written.

WHAT IT TOUCHES
---------------
Only ``InvoiceItem`` rows with ``item_id IS NULL`` whose ``item_name`` matches a
Treatment name exactly, case- and whitespace-insensitively, where that Treatment
has a ``catalog_item``. Matching is on the name alone because ``treatment_id``
was never persisted on the row — the payload carried it, the model has no column
for it.

Deliberately NOT touched:

* Names matching no Treatment — 'Daily Skin Sunscreen' (93 rows) is a product
  that has never existed in this catalogue, and one row has an empty name.
  Guessing a link for those would attach revenue to the wrong account.
* ``item_name`` is left in place rather than blanked. The invoice views write
  '' alongside a set ``item_id``, but keeping the original text on repaired rows
  is the only record of what the cashier actually typed, and no reader prefers
  ``item_name`` when ``item_id`` is present.

NO LEDGER EFFECT
----------------
This changes which account *future* postings route to, not any posted entry. To
move revenue already sitting in 4200000 onto the per-category accounts, re-post:
``python manage.py rebuild_ledger --from 2026-06-01`` (or the narrower
``unpost_month``) and run a journal sweep. Migration 0077 carries the same note.
"""
from django.db import migrations


def link_lines(apps, schema_editor):
    InvoiceItem = apps.get_model('managementsys', 'InvoiceItem')
    Treatment = apps.get_model('managementsys', 'Treatment')

    unlinked = list(
        InvoiceItem.objects
        .filter(item__isnull=True)
        .exclude(item_name='')
        .exclude(item_name__isnull=True)
        .values_list('id', 'item_name')
    )
    if not unlinked:
        return

    wanted = {(name or '').strip().lower() for _pk, name in unlinked}
    wanted.discard('')

    catalog_by_name = {}
    for name, catalog_item_id in Treatment.objects.filter(
        catalog_item__isnull=False,
    ).values_list('name', 'catalog_item_id'):
        key = (name or '').strip().lower()
        if key in wanted:
            # First writer wins. Two Treatments differing only in case would be a
            # data problem of their own; picking either is better than raising
            # inside a migration.
            catalog_by_name.setdefault(key, catalog_item_id)

    by_catalog_item = {}
    for pk, name in unlinked:
        catalog_item_id = catalog_by_name.get((name or '').strip().lower())
        if catalog_item_id is not None:
            by_catalog_item.setdefault(catalog_item_id, []).append(pk)

    linked = 0
    for catalog_item_id, ids in by_catalog_item.items():
        for start in range(0, len(ids), 500):
            linked += InvoiceItem.objects.filter(
                id__in=ids[start:start + 500],
            ).update(item_id=catalog_item_id)

    unmatched = len(unlinked) - linked
    print(
        f'\n  InvoiceItem treatment links: {linked} repaired, '
        f'{unmatched} left unlinked (name matches no Treatment with a catalog item).'
    )


def unlink_lines(apps, schema_editor):
    # Irreversible by design. Once these rows carry an item_id they are
    # indistinguishable from lines that were always linked, and unlinking by
    # name would also strip the ones written correctly. Migration 0077 made the
    # same call for the same reason.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0105_stock_correction_accounting'),
    ]

    operations = [
        migrations.RunPython(link_lines, unlink_lines),
    ]
