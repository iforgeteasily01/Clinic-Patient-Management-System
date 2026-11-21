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
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0105_stock_correction_accounting'),
    ]

    operations = [
        migrations.RunPython(link_lines, unlink_lines),
    ]
