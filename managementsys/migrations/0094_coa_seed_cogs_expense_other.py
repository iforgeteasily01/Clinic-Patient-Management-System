from django.db import migrations

HEADS = {
    'cogs':          5000000,
    'expense':       6000000,
    'other_income':  7000000,
    'other_expense': 8000000,
}

ACCOUNTS = [
    (5000200, 'Pemakaian Obat Treatment',                            'cogs'),
    (5000900, 'Koreksi/Obat Rusak/ED/dr. Melia',                     'cogs'),

    (6100005, 'Barang Habis Pakai Ruang Facial (Obat)',              'expense'),
    (6100010, 'Barang Habis Pakai Ruang Facial (Penunjang)',         'expense'),
    (6100020, 'Barang Habis Pakai Ruang Klinik Umum',                'expense'),
    (6100030, 'Obat Kirana',                                         'expense'),

    (6300010, 'Biaya Promosi/Iklan',                                 'expense'),
    (6300020, 'Biaya Komisi',                                        'expense'),
    (6300030, 'Biaya Entertainment',                                 'expense'),
    (6300900, 'Biaya Pemasaran Lainnya',                             'expense'),

    (6400010, 'Biaya Gaji, Lembur & THR',                            'expense'),
    (6400020, 'Biaya Bonus Pesangon & Kompensasi',                   'expense'),
    (6400030, 'Uang Makan Karyawan (Pak Wahyu)',                     'expense'),
    (6400040, 'Biaya Upah & Honorer',                                'expense'),
    (6400050, 'Biaya Catering & Makan Karyawan',                     'expense'),
    (6400060, 'BPJS Kesehatan',                                      'expense'),
    (6400070, 'BPJS Ketenagakerjan',                                 'expense'),
    (6400080, 'Pajak PPh 21',                                        'expense'),

    (6500050, 'Biaya Berlangganan dan Aktivasi Software Computer',   'expense'),
    (6500110, 'Biaya Listrik',                                       'expense'),
    (6500120, 'Biaya Air PAM',                                       'expense'),
    (6500130, 'Biaya Telepon/internet/Storage',                      'expense'),
    (6500150, 'Biaya Ekspedisi, Pengemasan, Pos & Materai',          'expense'),
    (6500160, 'BBM Kendaraan Expander',                              'expense'),
    (6500170, 'Kebersihan Klinik, Ruang dan Lingkungan',             'expense'),
    (6500175, 'Biaya Pembuangan Sampah Medis',                       'expense'),
    (6500180, 'Biaya Alat Pemadam Api Ringan',                       'expense'),
    (6500190, 'Biaya Konsultan Pajak',                               'expense'),
    (6500200, 'PPh Final (PP 55)',                                   'expense'),
    (6500205, 'Biaya Perijinan dan Pengurusan Surat',                'expense'),
    (6500210, 'Konsumsi',                                            'expense'),
    (6500220, 'Air Minum',                                           'expense'),
    (6500230, 'Biaya Retribusi/Partisipasi Lingkungan',              'expense'),
    (6500240, 'Barang Cetakan/ ATK, Tinta, Asesoris Comp',           'expense'),
    (6599900, 'Biaya Umum & Adm Lainnya',                            'expense'),

    (6600010, 'Biaya Pemeliharaan Gedung',                           'expense'),
    (6600020, 'Biaya Pemeliharaan inventaris',                       'expense'),
    (6600030, 'Biaya Pemeliharaan Kendaraan',                        'expense'),
    (6600040, 'Biaya Pemeliharaan Instalasi dan Penerangan Listrik', 'expense'),

    (6910000, 'Biaya Penyusutan Gedung',                             'expense'),
    (6920000, 'Biaya Penyusutan Kendaraan',                          'expense'),
    (6930000, 'Biaya Penyusutan Peralatan',                          'expense'),
    (6940010, 'Biaya Penyusutan Inventaris Gol. 1',                  'expense'),
    (6940020, 'Biaya Penyusutan Inventaris Gol. 2',                  'expense'),

    (7100010, 'Pendapatan Jasa Giro/Bunga Simpanan Bank',            'other_income'),
    (7100020, 'Pendapatan Bunga Deposito',                           'other_income'),
    (7100030, 'Penjualan Inventory / Perlengkapan',                  'other_income'),
    (7100040, 'Pendapatan Pendaftaran/Suket Pasien',                 'other_income'),
    (7100990, 'Pendapatan Lain-Lain',                                'other_income'),

    (8200010, 'Biaya Bunga Pinjaman Lainnya',                        'other_expense'),
    (8200020, 'Biaya Adm Bank & Buku Cek/Giro',                      'other_expense'),
    (8200030, 'Pajak Bunga Bank/ Jasa Giro',                         'other_expense'),
    (8200040, 'Selisih Pembulatan Rupiah',                           'other_expense'),
    (8200990, 'Pengeluaran Lain-lain',                               'other_expense'),
]


def forward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')

    head_names = {
        'cogs':          'Cost of Goods Sold',
        'expense':       'Expenses',
        'other_income':  'Other Income',
        'other_expense': 'Other Expenses',
    }
    heads = {}
    for acct_type, number in HEADS.items():
        head = COA.objects.filter(account_number=number, is_head=True).first()
        if head is None:
            head, _ = COA.objects.get_or_create(
                account_number=number,
                defaults={
                    'name': head_names[acct_type],
                    'account_type': acct_type,
                    'balance': 0,
                    'is_system': False,
                    'is_head': True,
                    'parent': None,
                },
            )
        heads[acct_type] = head

    existing = set(
        COA.objects
        .filter(account_number__in=[n for n, _, _ in ACCOUNTS])
        .values_list('account_number', flat=True)
    )

    COA.objects.bulk_create([
        COA(
            account_number=number,
            name=name,
            account_type=acct_type,
            balance=0,
            is_system=False,
            is_head=False,
            parent=heads[acct_type],
        )
        for number, name, acct_type in ACCOUNTS
        if number not in existing
    ])


def backward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')

    (
        COA.objects
        .filter(
            account_number__in=[n for n, _, _ in ACCOUNTS],
            is_system=False,
            is_head=False,
        )
        .filter(ledger_entries__isnull=True)
        .delete()
    )


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0093_remove_expense_payee'),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=backward),
    ]
