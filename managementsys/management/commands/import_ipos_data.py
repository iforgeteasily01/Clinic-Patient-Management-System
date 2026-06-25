"""
Import historical data from the legacy iPos PostgreSQL database into CPMS.

Usage:
    python manage.py import_ipos_data --dry-run              # preview only
    python manage.py import_ipos_data --phase services       # services → Treatments
    python manage.py import_ipos_data --phase inventory      # items → InventoryItem + InventoryBatch
    python manage.py import_ipos_data --phase sales          # transactions → Invoice + InvoiceItem
    python manage.py import_ipos_data --phase all            # everything

    python manage.py import_ipos_data --phase sales --from-date 2025-01-01

Mapping notes:
  - iPos kodesupel  ==  CPMS patient_no  (the iPos sync copies codes verbatim)
  - iPos kodeitem   ==  CPMS InventoryItem.code / Treatment.code  (1:1 codes)
  - Invoice number  :   IPOS-{notransaksi}  (identifies legacy records; avoids counter collision)
  - payment_method  :   null  (CPMS COA has no payment accounts yet)
  - cashier         :   null
  - warehouse       :   first active Warehouse found in CPMS (WH-01)
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

# iPos stores datetimes as local Jakarta time (GMT+7) without timezone info.
# Attaching this offset lets Django convert correctly to UTC on save.
TZ_JAKARTA = timezone(timedelta(hours=7))

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from managementsys.models import (
    ChartOfAccounts,
    InventoryBatch,
    InventoryItem,
    Invoice,
    InvoiceItem,
    Patient,
    Treatment,
    TreatmentCategory,
    Warehouse,
)
from managementsys.views.crm_page import refresh_crm_profile

# ── Treatment category auto-assignment ────────────────────────────────────────
# Checked against the item name (lowercase).  First match wins.
CATEGORY_RULES: list[tuple[str, str]] = [
    # pattern            category name
    (r'injeksi|infus|vaksin|suntik|kil\b', 'Injeksi'),
    (r'botox|filler|rejuran|nucleofil|placenta|skin\s*booster|collagen\s*simul', 'Injeksi'),
    (r'peel|peeling|mela.peel|tca|mask\s*peel', 'Peeling'),
    (r'\brf\b|hifu|micro\s*needle\s*rf|apollo', 'RF'),
    (r'laser|co2\s*(f|frac|plasma)|pico|ipl|plasma|dermapen|photo\s*rejuv|pdt\s*red|red\s*light', 'Laser & Alat'),
    (r'hair\s*removal|hr\s*(ketiak|kaki|kumis|v\b|removal)|wax', 'Hair Removal'),
    (r'facial|oxygen|masker|diamond\s*tambahan', 'Facial'),
    (r'thread|aptos|eyelid|stitching|operasi\s*kecil', 'Bedah Minimal'),
    (r'slim|slimming|body\s*contour|lipo', 'Slimming'),
    (r'konsul|konsultasi', 'Konsultasi'),
    (r'lab|gula\s*darah|kolesterol|asam\s*urat|ana\b|anti\s*ds|raf\b', 'Laboratorium'),
    (r'kartu|administrasi|surat\s*keterangan|lepas\s*jahitan|perawatan\s*luka|cek\b', 'Administrasi'),
]

_COMPILED_RULES = [(re.compile(pat, re.I), cat) for pat, cat in CATEGORY_RULES]

FALLBACK_CATEGORY = 'Lain-lain'


def _guess_category(name: str) -> str:
    for pattern, cat in _COMPILED_RULES:
        if pattern.search(name):
            return cat
    return FALLBACK_CATEGORY


def _ipos_conn():
    """Open a raw psycopg2 connection to the iPos database using Django's external DB config."""
    cfg = connections['external'].settings_dict
    import psycopg2
    return psycopg2.connect(
        dbname=cfg['NAME'],
        user=cfg['USER'],
        password=cfg['PASSWORD'],
        host=cfg['HOST'],
        port=cfg['PORT'],
    )


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v)) if v is not None else Decimal('0')
    except InvalidOperation:
        return Decimal('0')


# ── Command ────────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = 'Import legacy iPos data (services, inventory, sales) into CPMS.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--phase',
            choices=['services', 'inventory', 'sales', 'all'],
            default='all',
            help='Which import phase to run (default: all)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print what would be imported without writing anything',
        )
        parser.add_argument(
            '--from-date',
            default=None,
            metavar='YYYY-MM-DD',
            help='Only import sales on or after this date (sales phase)',
        )

    def handle(self, *args, **options):
        self.dry = options['dry_run']
        self.phase = options['phase']
        self.from_date = options['from_date']
        self.verbosity = options['verbosity']

        if self.dry:
            self.stdout.write(self.style.WARNING('DRY RUN — nothing will be written\n'))

        conn = _ipos_conn()
        try:
            if self.phase in ('services', 'all'):
                self._import_services(conn)
            if self.phase in ('inventory', 'all'):
                self._import_inventory(conn)
            if self.phase in ('sales', 'all'):
                self._import_sales(conn)
        finally:
            conn.close()

        self.stdout.write(self.style.SUCCESS('\nDone.'))

    # ── Phase 1 — Services → Treatment ────────────────────────────────────────

    def _import_services(self, conn):
        self.stdout.write('\n=== Phase 1: Services -> Treatments ===')
        cur = conn.cursor()
        cur.execute("""
            SELECT kodeitem, namaitem, hargajual1, statusjual
            FROM tbl_item
            WHERE tipe = '2'
            ORDER BY namaitem
        """)
        rows = cur.fetchall()
        cur.close()

        self.stdout.write(f'  iPos service items: {len(rows)}')

        # Build category cache (create missing ones)
        cat_cache: dict[str, TreatmentCategory] = {
            tc.name: tc for tc in TreatmentCategory.objects.all()
        }

        # Existing treatment codes to skip
        existing_codes = set(Treatment.objects.values_list('code', flat=True))

        created = skipped = errors = 0

        for kodeitem, namaitem, hargajual1, statusjual in rows:
            if kodeitem in existing_codes:
                if self.verbosity >= 2:
                    self.stdout.write(f'    SKIP (exists): {kodeitem}')
                skipped += 1
                continue

            cat_name = _guess_category(namaitem or kodeitem)

            if not self.dry:
                # Get or create TreatmentCategory
                if cat_name not in cat_cache:
                    tc = TreatmentCategory.objects.create(name=cat_name)
                    cat_cache[cat_name] = tc
                    self.stdout.write(f'    Created category: {cat_name!r}')
                cat_obj = cat_cache[cat_name]

                try:
                    with transaction.atomic():
                        Treatment.objects.create(
                            code=kodeitem,
                            name=namaitem or kodeitem,
                            category=cat_obj,
                            price=_dec(hargajual1),
                            active=(statusjual == 'Y'),
                        )
                    created += 1
                    if self.verbosity >= 2:
                        self.stdout.write(f'    CREATE: {kodeitem!r} [{cat_name}] price={hargajual1}')
                except Exception as e:
                    errors += 1
                    self.stderr.write(f'    ERROR {kodeitem!r}: {e}')
            else:
                # Dry run — report what would be created
                if cat_name not in cat_cache:
                    self.stdout.write(f'    Would create category: {cat_name!r}')
                    cat_cache[cat_name] = None  # type: ignore[assignment]
                self.stdout.write(
                    f'    Would CREATE Treatment: {kodeitem!r} [{cat_name}] '
                    f'name={namaitem!r} price={hargajual1} active={statusjual == "Y"}'
                )
                created += 1

        self.stdout.write(
            f'  Services: {created} {"would be " if self.dry else ""}created, '
            f'{skipped} skipped, {errors} errors'
        )

    # ── Phase 2 — Physical goods → InventoryItem + opening InventoryBatch ─────

    def _import_inventory(self, conn):
        self.stdout.write('\n=== Phase 2: Inventory items + opening stock ===')  # noqa
        cur = conn.cursor()

        # All physical goods
        cur.execute("""
            SELECT i.kodeitem, i.namaitem, i.hargajual1, i.hargapokok, i.satuan,
                   COALESCE(s_gd.stok, 0)  AS stok_gd,
                   COALESCE(s_utm.stok, 0) AS stok_utm
            FROM tbl_item i
            LEFT JOIN tbl_itemstok s_gd  ON i.kodeitem = s_gd.kodeitem  AND s_gd.kantor  = 'GD'
            LEFT JOIN tbl_itemstok s_utm ON i.kodeitem = s_utm.kodeitem AND s_utm.kantor = 'UTM'
            WHERE i.tipe = '1' AND i.statusjual = 'Y'
            ORDER BY i.kodeitem
        """)
        rows = cur.fetchall()
        cur.close()

        self.stdout.write(f'  iPos physical goods: {len(rows)}')

        warehouse = Warehouse.objects.filter(is_active=True).first()
        if not warehouse:
            self.stderr.write('  ERROR: No active warehouse found in CPMS. Create one first.')
            return

        existing_codes = set(
            InventoryItem.objects.filter(is_service=False).values_list('code', flat=True)
        )

        items_created = items_skipped = batch_created = errors = 0

        for kodeitem, namaitem, hargajual1, hargapokok, satuan, stok_gd, stok_utm in rows:
            total_stock = _dec(stok_gd) + _dec(stok_utm)

            if kodeitem in existing_codes:
                items_skipped += 1
                # Still create opening batch if stock exists and not dry run
                if total_stock > 0 and not self.dry:
                    try:
                        item = InventoryItem.objects.get(code=kodeitem)
                        already_has_batch = InventoryBatch.objects.filter(item=item).exists()
                        if not already_has_batch:
                            InventoryBatch.objects.create(
                                item=item,
                                warehouse=warehouse,
                                input_date=date.today(),
                                quantity_initial=total_stock,
                                quantity_remaining=total_stock,
                                value=_dec(hargapokok) * total_stock,
                            )
                            batch_created += 1
                    except Exception as e:
                        errors += 1
                        self.stderr.write(f'    ERROR batch for {kodeitem!r}: {e}')
                continue

            if self.dry:
                self.stdout.write(
                    f'    Would CREATE item: {kodeitem!r} {namaitem!r} '
                    f'price={hargajual1} cost={hargapokok} stock={total_stock}'
                )
                items_created += 1
                if total_stock > 0:
                    batch_created += 1
                continue

            try:
                with transaction.atomic():
                    item = InventoryItem.objects.create(
                        code=kodeitem,
                        name=namaitem or kodeitem,
                        selling_price=_dec(hargajual1),
                        unit_small=satuan or 'pcs',
                        is_service=False,
                        is_active=True,
                    )
                    items_created += 1
                    existing_codes.add(kodeitem)

                    if total_stock > 0:
                        InventoryBatch.objects.create(
                            item=item,
                            warehouse=warehouse,
                            input_date=date.today(),
                            quantity_initial=total_stock,
                            quantity_remaining=total_stock,
                            value=_dec(hargapokok) * total_stock,
                        )
                        batch_created += 1
            except Exception as e:
                errors += 1
                self.stderr.write(f'    ERROR {kodeitem!r}: {e}')

        self.stdout.write(
            f'  Items: {items_created} {"would be " if self.dry else ""}created, '
            f'{items_skipped} skipped, {batch_created} batches, {errors} errors'
        )

    # ── Phase 3 — Sales → Invoice + InvoiceItem ───────────────────────────────

    def _import_sales(self, conn):
        self.stdout.write('\n=== Phase 3: Sales -> Invoices ===')

        warehouse = Warehouse.objects.filter(is_active=True).first()
        if not warehouse:
            self.stderr.write('  ERROR: No active warehouse. Create one first.')
            return

        # Build lookup: iPos kodeitem → CPMS InventoryItem pk
        item_map: dict[str, InventoryItem] = {
            item.code: item
            for item in InventoryItem.objects.all()
        }
        # Build patient set for fast membership check
        patient_set: set[str] = set(
            Patient.objects.values_list('patient_no', flat=True)
        )

        # Existing IPOS invoice numbers to skip (idempotent re-runs)
        existing_inv_nos: set[str] = set(
            Invoice.objects.filter(invoice_number__startswith='IPOS-')
                           .values_list('invoice_number', flat=True)
        )

        # Build date filter
        date_filter = ''
        date_params: list = []
        if self.from_date:
            try:
                datetime.strptime(self.from_date, '%Y-%m-%d')
            except ValueError:
                raise CommandError(f'Invalid --from-date: {self.from_date!r}. Use YYYY-MM-DD.')
            date_filter = "AND h.tanggal >= %s"
            date_params = [self.from_date]

        cur = conn.cursor()
        cur.execute(f"""
            SELECT h.notransaksi, h.kodekantor, h.tanggal, h.kodesupel,
                   h.potfaktur, h.pajak, h.biayalain, h.totalakhir, h.keterangan
            FROM tbl_ikhd h
            WHERE h.tipe IN ('KSR', 'IK')
            {date_filter}
            ORDER BY h.tanggal
        """, date_params)
        headers = cur.fetchall()
        self.stdout.write(f'  iPos sale transactions to process: {len(headers)}')

        inv_created = inv_skipped = inv_errors = line_created = line_skipped = 0
        affected_patients: set[str] = set()

        for notransaksi, kodekantor, tanggal, kodesupel, potfaktur, pajak, biayalain, totalakhir, keterangan in headers:
            inv_no = f'IPOS-{notransaksi}'

            if inv_no in existing_inv_nos:
                inv_skipped += 1
                continue

            # Resolve patient — iPos kodesupel = CPMS patient_no directly
            patient: Patient | None = None
            if kodesupel and kodesupel in patient_set:
                try:
                    patient = Patient.objects.get(patient_no=kodesupel)
                except Patient.DoesNotExist:
                    pass

            # Fetch line items
            cur2 = conn.cursor()
            cur2.execute("""
                SELECT d.kodeitem, i.namaitem, d.jumlah, d.harga, d.total
                FROM tbl_ikdt d
                LEFT JOIN tbl_item i ON d.kodeitem = i.kodeitem
                WHERE d.notransaksi = %s
            """, [notransaksi])
            lines = cur2.fetchall()
            cur2.close()

            # Attach Jakarta timezone so Django stores the correct UTC value
            tanggal_aware = tanggal.replace(tzinfo=TZ_JAKARTA) if tanggal else None

            if self.dry:
                patient_label = patient.name if patient else f'UNKNOWN({kodesupel})'
                self.stdout.write(
                    f'    Would CREATE INV {inv_no} | {tanggal_aware} | patient={patient_label} '
                    f'| lines={len(lines)} | total={totalakhir}'
                )
                inv_created += 1
                line_created += len(lines)
                if patient:
                    affected_patients.add(kodesupel)
                continue

            try:
                with transaction.atomic():
                    inv = Invoice(
                        invoice_number=inv_no,
                        datetime=tanggal_aware,
                        patient_no=patient,
                        payment_method=None,
                        discount=_dec(potfaktur),
                        tax=_dec(pajak),
                        additional_charges=_dec(biayalain),
                        grand_total=_dec(totalakhir),
                        cashier=None,
                        warehouse=warehouse,
                        notes=(keterangan or '').strip()[:500],
                    )
                    inv.save()  # uses custom save() for invoice_number — overridden by pre-set value
                    existing_inv_nos.add(inv_no)
                    inv_created += 1
                    if patient:
                        affected_patients.add(kodesupel)

                    items_to_create = []
                    for kodeitem, namaitem, jumlah, harga, total in lines:
                        inv_item = item_map.get(kodeitem)
                        items_to_create.append(InvoiceItem(
                            invoice=inv,
                            item=inv_item,
                            item_name=(namaitem or kodeitem or '')[:255],
                            quantity=_dec(jumlah),
                            price=_dec(harga),
                            discount_pct=Decimal('0'),
                        ))
                        if inv_item:
                            line_created += 1
                        else:
                            line_skipped += 1

                    InvoiceItem.objects.bulk_create(items_to_create)

            except Exception as e:
                inv_errors += 1
                self.stderr.write(f'    ERROR {notransaksi!r}: {e}')

        cur.close()

        self.stdout.write(
            f'  Invoices: {inv_created} {"would be " if self.dry else ""}created, '
            f'{inv_skipped} skipped, {inv_errors} errors'
        )
        self.stdout.write(
            f'  Invoice lines: {line_created} matched to items, {line_skipped} item not in CPMS (name stored)'
        )
        self.stdout.write(f'  Affected patients: {len(affected_patients)}')

        if not self.dry and affected_patients:
            self.stdout.write('  Refreshing CRM profiles…')
            for patient_no in affected_patients:
                try:
                    p = Patient.objects.get(patient_no=patient_no)
                    refresh_crm_profile(p)
                except Patient.DoesNotExist:
                    pass
            self.stdout.write(f'  CRM profiles refreshed for {len(affected_patients)} patients')
