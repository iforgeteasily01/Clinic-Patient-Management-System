"""
managementsys/services/manual_journal.py

Manual journal entries: the catalog of transaction shapes an operator can post
by hand, the classifier that names the shape they just composed, and the date
window they are allowed to post into.

Why this module exists
----------------------
The old ``POST /api/accounting/adjustments/`` wrote a *single* LedgerEntry row.
Two things were wrong with that:

* One row cannot balance. Every such adjustment pushed the trial balance out by
  its own amount, permanently, with nothing to net it back.
* It moved the cached balance by ``+amount`` for a debit and ``-amount`` for a
  credit regardless of the account's normal balance, so a credit to a revenue or
  liability account moved that account the wrong way (compare ``_apply_balance``
  in ``journal_engine``, which gets this right).

A manual entry is now a real journal document: at least two lines, debits equal
credits, one ``JournalEntry`` header, written through ``write_legs`` like every
other posting from Phase 4 on.

Classification
--------------
Double entry is mechanical but not self-explaining — an operator who knows they
received money does not necessarily know that "Dr asset / Cr revenue" is the
shape that records it. ``classify`` reads the account *types* on each side and
names the transaction, so the composer can say "this is a Sale" before anything
is posted. The catalog it reads is also served to the UI as reference material,
which is what keeps the guide page and the classifier from drifting apart.

Classification is advisory. It never blocks a post: an entry the catalog does not
recognise is still valid double entry, and refusing it would make the catalog a
whitelist that has to anticipate every legitimate transaction. It is reported as
unrecognised so the operator can look twice.

Rules narrow by number *band*, not by exact account number, because the accounts
worth naming are families rather than single rows — per-vendor AP sub-accounts
are 2100001+ under the 2100000 control, and every cash/bank/e-wallet account is
a child of 1100000 (see ``services/cash_accounts.py``, which documents that band
as the definition). A band is pure data, so no rule needs a query to match.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from ..models import ChartOfAccounts, JournalDayLog


# ── Account-type vocabulary ───────────────────────────────────────────────────
# Mirrors ChartOfAccounts.account_type. Indonesian labels because every
# accounting surface in this project is Indonesian.

TYPE_LABELS = {
    'asset':         'Aset',
    'liability':     'Kewajiban',
    'equity':        'Ekuitas',
    'revenue':       'Pendapatan',
    'cogs':          'Beban Pokok Penjualan',
    'expense':       'Beban Operasional',
    'other_income':  'Pendapatan Lain-lain',
    'other_expense': 'Beban Lain-lain',
}

# ── Account families, as inclusive number bands ───────────────────────────────
# Numbers follow the live chart (seed 0013 as amended by 0073/0075/0076/0100),
# not the original seed: 2200000 is Utang Pajak since 0075, and AP sub-accounts
# per vendor start at 2100001 (0073).

BAND_CASH       = (1100001, 1199999)   # cash / bank / e-wallet, children of 1100000
BAND_AR         = (1200000, 1209999)   # accounts receivable
BAND_INVENTORY  = (1300000, 1319999)   # inventory — products and treatment supplies
BAND_PREPAID    = (1400000, 1409999)   # prepaid expenses
BAND_FIXED      = (1500000, 1599999)   # equipment and accumulated depreciation
BAND_AP         = (2100000, 2100999)   # AP control + per-vendor sub-accounts
BAND_TAX        = (2200000, 2200999)   # Utang Pajak
BAND_OPENING_EQ = (3900000, 3900999)   # Opening Balance Equity

BAND_LABELS = {
    BAND_CASH:       'Kas / Bank',
    BAND_AR:         'Piutang Usaha',
    BAND_INVENTORY:  'Persediaan',
    BAND_PREPAID:    'Biaya Dibayar di Muka',
    BAND_FIXED:      'Aset Tetap',
    BAND_AP:         'Utang Usaha',
    BAND_TAX:        'Utang Pajak',
    BAND_OPENING_EQ: 'Ekuitas Saldo Awal',
}


def _in_bands(numbers, bands):
    """True when at least one of ``numbers`` falls inside one of ``bands``."""
    return any(lo <= n <= hi for n in numbers for lo, hi in bands)


@dataclass(frozen=True)
class TransactionType:
    """One recognised transaction shape.

    ``debit``/``credit`` are the sets of account types the shape puts on each
    side. ``debit_bands``/``credit_bands`` optionally require a specific account
    family on that side, which makes a rule outrank a type-only rule covering
    the same pair — that is how "pay down a supplier bill" is distinguished from
    the generic "settle a liability" it is a special case of.
    """
    code: str
    name: str
    debit: frozenset
    credit: frozenset
    what: str
    example: str
    caution: str = ''
    debit_bands: tuple = ()
    credit_bands: tuple = ()

    @property
    def specificity(self):
        """Higher wins when several rules match the same entry."""
        return 1 + 10 * (len(self.debit_bands) + len(self.credit_bands))

    @property
    def shape(self):
        """'Dr Aset / Cr Pendapatan' — the shape, for display. Named families
        replace the bare type label when the rule requires one."""
        def side(types, bands):
            if bands:
                return ' + '.join(BAND_LABELS.get(b, '?') for b in bands)
            return ' + '.join(sorted(TYPE_LABELS.get(t, t) for t in types))
        return f'Dr {side(self.debit, self.debit_bands)} / Cr {side(self.credit, self.credit_bands)}'

    def as_dict(self):
        return {
            'code': self.code,
            'name': self.name,
            'shape': self.shape,
            'debit_types': sorted(self.debit),
            'credit_types': sorted(self.credit),
            'debit_type_labels': [TYPE_LABELS.get(t, t) for t in sorted(self.debit)],
            'credit_type_labels': [TYPE_LABELS.get(t, t) for t in sorted(self.credit)],
            'debit_family': [BAND_LABELS.get(b, '') for b in self.debit_bands],
            'credit_family': [BAND_LABELS.get(b, '') for b in self.credit_bands],
            'what': self.what,
            'example': self.example,
            'caution': self.caution,
        }


def _t(code, name, debit, credit, what, example, caution='',
       debit_bands=(), credit_bands=()):
    return TransactionType(
        code=code, name=name,
        debit=frozenset(debit), credit=frozenset(credit),
        what=what, example=example, caution=caution,
        debit_bands=tuple(debit_bands), credit_bands=tuple(credit_bands),
    )


# ── The catalog ───────────────────────────────────────────────────────────────
# Order matters: ties on specificity are broken by position, so the more common
# reading of an ambiguous shape comes first (a plain Dr asset / Cr revenue is far
# more often a cash sale than a credit sale).
#
# `caution` is where a shape is legitimate accounting but the wrong tool here,
# because a document elsewhere in the system already posts it — a manual copy
# would double-count.

TRANSACTION_TYPES = [
    # ── Money in ──────────────────────────────────────────────────────────────
    _t('sale', 'Penjualan tunai',
       ['asset'], ['revenue'],
       'Aset (kas/bank) naik karena jasa atau barang terjual, dan pendapatan '
       'diakui pada saat yang sama.',
       'Pasien membayar tindakan Rp 500.000 tunai: Dr Kas 500.000 / Cr Pendapatan 500.000.',
       caution='Penjualan normal sudah diposting otomatis dari invoice POS lewat '
               'Jalankan Jurnal. Pakai entri manual hanya untuk penjualan yang '
               'tidak pernah tercatat sebagai invoice.',
       debit_bands=[BAND_CASH]),

    _t('receivable_sale', 'Penjualan kredit (piutang)',
       ['asset'], ['revenue'],
       'Pendapatan diakui sekarang, uangnya diterima nanti. Yang naik adalah '
       'piutang, bukan kas.',
       'Tagihan ke perusahaan asuransi Rp 2.000.000 belum dibayar: '
       'Dr Piutang Usaha 2.000.000 / Cr Pendapatan 2.000.000.',
       debit_bands=[BAND_AR]),

    _t('receivable_collect', 'Penerimaan piutang',
       ['asset'], ['asset'],
       'Satu aset berubah menjadi aset lain: piutang berkurang, kas naik. Tidak '
       'ada pendapatan baru — pendapatannya sudah diakui saat penjualan.',
       'Asuransi melunasi Rp 2.000.000 ke bank: Dr Bank 2.000.000 / Cr Piutang Usaha 2.000.000.',
       caution='Jangan mengakui pendapatan lagi di sini; penjualan yang sama akan '
               'terhitung dua kali.',
       debit_bands=[BAND_CASH], credit_bands=[BAND_AR]),

    _t('other_income', 'Pendapatan lain-lain',
       ['asset'], ['other_income'],
       'Uang masuk yang bukan dari kegiatan utama klinik.',
       'Bunga bank Rp 35.000 masuk rekening: Dr Bank 35.000 / Cr Pendapatan Lain-lain 35.000.'),

    _t('customer_deposit', 'Uang muka / deposit pelanggan',
       ['asset'], ['liability'],
       'Uang sudah diterima tetapi jasanya belum diberikan, jadi belum boleh '
       'diakui sebagai pendapatan — masih kewajiban ke pasien.',
       'Pasien membayar paket 10 sesi Rp 5.000.000 di muka: '
       'Dr Kas 5.000.000 / Cr Pendapatan Diterima di Muka 5.000.000.',
       debit_bands=[BAND_CASH]),

    _t('deferred_earned', 'Pengakuan pendapatan diterima di muka',
       ['liability'], ['revenue'],
       'Jasa yang sudah dibayar di muka akhirnya diberikan: kewajiban turun, '
       'pendapatan diakui. Tidak ada kas yang bergerak.',
       'Satu dari 10 sesi paket dipakai: Dr Pendapatan Diterima di Muka 500.000 / '
       'Cr Pendapatan Tindakan 500.000.'),

    _t('capital_in', 'Setoran modal pemilik',
       ['asset'], ['equity'],
       'Pemilik memasukkan uang atau aset ke klinik. Ini bukan pendapatan.',
       'Pemilik menyetor Rp 50.000.000 ke rekening klinik: '
       'Dr Bank 50.000.000 / Cr Modal Pemilik 50.000.000.'),

    _t('opening_balance', 'Saldo awal',
       ['asset'], ['equity'],
       'Memasukkan saldo yang sudah ada sebelum sistem dipakai. Penyeimbangnya '
       'adalah ekuitas saldo awal, bukan pendapatan.',
       'Saldo kas awal Rp 25.000.000: Dr Kas 25.000.000 / Cr Ekuitas Saldo Awal 25.000.000.',
       credit_bands=[BAND_OPENING_EQ]),

    _t('loan_in', 'Penerimaan pinjaman',
       ['asset'], ['liability'],
       'Kas naik karena berutang, bukan karena menjual. Kewajiban naik sebesar '
       'pokok pinjaman.',
       'Pencairan kredit bank Rp 100.000.000: Dr Bank 100.000.000 / Cr Utang Bank 100.000.000.'),

    # ── Money out ─────────────────────────────────────────────────────────────
    _t('expense_cash', 'Beban dibayar tunai',
       ['expense'], ['asset'],
       'Biaya operasional dibayar langsung dari kas atau bank.',
       'Bayar listrik Rp 1.200.000 dari bank: Dr Beban Listrik 1.200.000 / Cr Bank 1.200.000.',
       caution='Beban rutin sebaiknya lewat menu Beban agar ada dokumen dan jejak '
               'pembayarannya.',
       credit_bands=[BAND_CASH]),

    _t('expense_accrue', 'Beban masih harus dibayar (akrual)',
       ['expense'], ['liability'],
       'Biaya sudah terjadi di periode ini tetapi belum dibayar. Diakui sekarang '
       'agar laba periode ini tidak terlihat terlalu tinggi.',
       'Gaji Agustus Rp 15.000.000 dibayar awal September: '
       'Dr Beban Gaji 15.000.000 / Cr Utang Gaji 15.000.000.'),

    _t('prepaid_amortise', 'Amortisasi biaya dibayar di muka',
       ['expense'], ['asset'],
       'Biaya yang dibayar sekaligus di muka dibebankan sedikit-sedikit setiap '
       'periode sesuai masa manfaatnya.',
       'Sewa setahun Rp 120.000.000 dibebankan untuk Agustus: '
       'Dr Beban Sewa 10.000.000 / Cr Sewa Dibayar di Muka 10.000.000.',
       credit_bands=[BAND_PREPAID]),

    _t('depreciation', 'Penyusutan aset tetap',
       ['expense'], ['asset'],
       'Sebagian nilai aset tetap dibebankan ke periode ini. Kas tidak bergerak.',
       'Penyusutan alat laser sebulan Rp 1.333.333: '
       'Dr Beban Penyusutan 1.333.333 / Cr Akumulasi Penyusutan 1.333.333.',
       credit_bands=[BAND_FIXED]),

    _t('supplier_pay', 'Pembayaran utang supplier',
       ['liability'], ['asset'],
       'Melunasi tagihan supplier: Utang Usaha per vendor turun, kas turun.',
       'Bayar faktur supplier Rp 8.000.000: Dr Utang Usaha — Vendor 8.000.000 / Cr Bank 8.000.000.',
       caution='Pakai tombol Bayar pada faktur pembelian, bukan entri manual — '
               'pembayaran manual tidak mengubah status faktur menjadi lunas.',
       debit_bands=[BAND_AP], credit_bands=[BAND_CASH]),

    _t('tax_pay', 'Penyetoran pajak',
       ['liability'], ['asset'],
       'Menyetor pajak yang sudah dipungut atau terutang ke kas negara.',
       'Setor PPN Rp 4.400.000: Dr Utang Pajak 4.400.000 / Cr Bank 4.400.000.',
       debit_bands=[BAND_TAX], credit_bands=[BAND_CASH]),

    _t('liability_pay', 'Pembayaran utang',
       ['liability'], ['asset'],
       'Kewajiban dilunasi dengan kas. Tidak ada beban baru — bebannya sudah '
       'diakui saat akrual atau saat pembelian.',
       'Bayar utang gaji Rp 15.000.000: Dr Utang Gaji 15.000.000 / Cr Bank 15.000.000.'),

    _t('other_expense', 'Beban lain-lain',
       ['other_expense'], ['asset'],
       'Pengeluaran di luar kegiatan utama klinik.',
       'Biaya administrasi bank Rp 25.000: Dr Beban Administrasi Bank 25.000 / Cr Bank 25.000.'),

    _t('capital_out', 'Pengambilan modal / prive',
       ['equity'], ['asset'],
       'Pemilik mengambil uang klinik untuk keperluan pribadi. Ini bukan beban '
       'dan tidak boleh mengurangi laba.',
       'Pemilik mengambil Rp 10.000.000: Dr Prive Pemilik 10.000.000 / Cr Bank 10.000.000.'),

    # ── Aset & persediaan ─────────────────────────────────────────────────────
    _t('asset_transfer', 'Pindah saldo antar kas/bank',
       ['asset'], ['asset'],
       'Memindahkan uang antar kas, bank atau e-wallet. Total aset tidak berubah.',
       'Setor kas laci Rp 5.000.000 ke bank: Dr Bank 5.000.000 / Cr Kas 5.000.000.',
       caution='Pakai menu Transfer Rekening — entri manual tidak membuat catatan '
               'transfer yang bisa dilacak.',
       debit_bands=[BAND_CASH], credit_bands=[BAND_CASH]),

    _t('asset_buy_cash', 'Pembelian aset tetap tunai',
       ['asset'], ['asset'],
       'Kas berubah bentuk menjadi aset lain. Belum ada beban — beban muncul '
       'kemudian lewat penyusutan.',
       'Beli alat laser Rp 80.000.000 tunai: Dr Peralatan 80.000.000 / Cr Bank 80.000.000.',
       debit_bands=[BAND_FIXED], credit_bands=[BAND_CASH]),

    _t('prepaid_pay', 'Pembayaran biaya di muka',
       ['asset'], ['asset'],
       'Membayar sekaligus untuk manfaat beberapa periode ke depan. Belum jadi '
       'beban sekarang; dibebankan bertahap lewat amortisasi.',
       'Bayar sewa setahun Rp 120.000.000: '
       'Dr Sewa Dibayar di Muka 120.000.000 / Cr Bank 120.000.000.',
       debit_bands=[BAND_PREPAID], credit_bands=[BAND_CASH]),

    _t('inventory_buy_credit', 'Pembelian persediaan kredit',
       ['asset'], ['liability'],
       'Stok masuk, dibayar nanti: persediaan naik, Utang Usaha naik.',
       'Terima 100 botol serum Rp 20.000.000 belum dibayar: '
       'Dr Persediaan 20.000.000 / Cr Utang Usaha — Vendor 20.000.000.',
       caution='Ini yang diposting otomatis oleh faktur pembelian. Entri manual '
               'akan menghitung stok dua kali dan tidak membuat batch FIFO.',
       debit_bands=[BAND_INVENTORY], credit_bands=[BAND_AP]),

    _t('cogs_recognise', 'Pengakuan beban pokok penjualan',
       ['cogs'], ['asset'],
       'Persediaan yang terpakai atau terjual dipindahkan dari aset menjadi '
       'beban pokok penjualan.',
       'Pemakaian bahan tindakan sebulan Rp 12.000.000: '
       'Dr HPP 12.000.000 / Cr Persediaan 12.000.000.',
       caution='HPP produk sudah diposting otomatis dari invoice lewat FIFO. Entri '
               'manual dipakai untuk biaya bahan tindakan, yang memang dicatat '
               'berkala (lihat migrasi 0107).',
       credit_bands=[BAND_INVENTORY]),

    _t('inventory_writeoff', 'Penghapusan / kerugian persediaan',
       ['other_expense'], ['asset'],
       'Stok hilang, kedaluwarsa atau rusak. Nilainya keluar dari aset menjadi '
       'kerugian, bukan HPP — barangnya tidak terjual.',
       'Serum kedaluwarsa Rp 900.000 dibuang: '
       'Dr Kerugian Persediaan 900.000 / Cr Persediaan 900.000.',
       caution='Selisih hasil stok opname sebaiknya lewat menu Stok Opname agar '
               'kuantitas batch ikut terkoreksi.',
       credit_bands=[BAND_INVENTORY]),

    # ── Koreksi ───────────────────────────────────────────────────────────────
    _t('sales_return', 'Pengembalian penjualan / refund',
       ['revenue'], ['asset'],
       'Penjualan dibatalkan setelah uang diterima: pendapatan dikurangi, kas keluar.',
       'Refund tindakan Rp 500.000: Dr Pendapatan (atau Diskon Penjualan) 500.000 / '
       'Cr Kas 500.000.',
       caution='Untuk invoice yang sudah diposting, pakai Void pada invoice — sistem '
               'membuat memo pembalik lengkap termasuk stok dan HPP.'),

    _t('reclass_expense', 'Reklasifikasi antar beban',
       ['expense'], ['expense'],
       'Biaya yang tercatat di akun yang salah dipindahkan ke akun yang benar. '
       'Total beban tidak berubah.',
       'Rp 800.000 salah masuk Beban Listrik, seharusnya Beban Air: '
       'Dr Beban Air 800.000 / Cr Beban Listrik 800.000.'),

    _t('reclass_revenue', 'Reklasifikasi antar pendapatan',
       ['revenue'], ['revenue'],
       'Pendapatan dipindahkan antar kategori. Total pendapatan tidak berubah.',
       'Rp 1.500.000 salah masuk Pendapatan Produk, seharusnya Pendapatan Facial: '
       'Dr Pendapatan Produk 1.500.000 / Cr Pendapatan Facial 1.500.000.'),

    _t('reclass_liability', 'Reklasifikasi antar kewajiban',
       ['liability'], ['liability'],
       'Kewajiban dipindahkan antar akun, misalnya dari utang jangka panjang ke '
       'bagian yang jatuh tempo tahun ini.',
       'Rp 24.000.000 pokok kredit jatuh tempo tahun depan: '
       'Dr Utang Bank Jangka Panjang 24.000.000 / Cr Utang Bank Jangka Pendek 24.000.000.'),

    _t('reclass_asset', 'Reklasifikasi antar aset',
       ['asset'], ['asset'],
       'Aset dipindahkan antar akun tanpa mengubah total aset.',
       'Rp 3.000.000 salah masuk Persediaan Produk, seharusnya Persediaan Bahan '
       'Tindakan: Dr Persediaan Bahan Tindakan 3.000.000 / Cr Persediaan Produk 3.000.000.'),
]

TYPES_BY_CODE = {t.code: t for t in TRANSACTION_TYPES}


def catalog():
    """The whole catalog as JSON-ready dicts, for the guide page."""
    return [t.as_dict() for t in TRANSACTION_TYPES]


# ── Classification ────────────────────────────────────────────────────────────

def _sides(legs):
    """Types and account numbers per side for ``legs``, each
    ``(account, entry_type, amount)``.

    Zero-amount and account-less legs are skipped: a blank row the operator has
    not filled in yet must not change what the entry is called.
    """
    dr_types, cr_types = set(), set()
    dr_nums, cr_nums = set(), set()
    for account, entry_type, amount in legs:
        if account is None or not amount:
            continue
        if entry_type == 'debit':
            dr_types.add(account.account_type)
            dr_nums.add(account.account_number)
        else:
            cr_types.add(account.account_type)
            cr_nums.add(account.account_number)
    return dr_types, cr_types, dr_nums, cr_nums


def _matches(rule, dr_types, cr_types, dr_nums, cr_nums):
    if rule.debit != dr_types or rule.credit != cr_types:
        return False
    if rule.debit_bands and not _in_bands(dr_nums, rule.debit_bands):
        return False
    if rule.credit_bands and not _in_bands(cr_nums, rule.credit_bands):
        return False
    return True


def _empty_result():
    return {
        'code': '', 'name': '', 'shape': '', 'what': '', 'example': '',
        'caution': '', 'confidence': 'unknown', 'alternatives': [],
    }


def _best_matches(dr_types, cr_types, dr_nums, cr_nums):
    """The catalog rules that fit, most specific first.

    Several equally specific rules can fit the same shape — Dr Kas / Cr
    Pendapatan is a cash sale, but a plain Dr Aset / Cr Pendapatan could be
    either a cash or a credit sale. Ties are broken by catalog order (the more
    common reading comes first) and the losers are returned too, so the caller
    can show them rather than assert one silently.
    """
    fits = [r for r in TRANSACTION_TYPES
            if _matches(r, dr_types, cr_types, dr_nums, cr_nums)]
    if not fits:
        return []
    top = max(r.specificity for r in fits)
    return [r for r in fits if r.specificity == top]


def _type_totals(legs):
    dr_totals, cr_totals = {}, {}
    for account, entry_type, amount in legs:
        if account is None or not amount:
            continue
        bucket = dr_totals if entry_type == 'debit' else cr_totals
        bucket[account.account_type] = bucket.get(account.account_type, Decimal('0')) + amount
    return dr_totals, cr_totals


def classify(legs):
    """Name the transaction ``legs`` describes.

    Returns the matched catalog entry as a dict plus:

    * ``confidence`` — ``'exact'`` when the type sets on both sides match a
      catalog entry; ``'compound'`` when the entry mixes several types per side
      and only the individual pairs are recognised; ``'unknown'`` when nothing
      matched (still postable, if it balances).
    * ``alternatives`` — the other readings: rival interpretations of the same
      shape when exact, the remaining component transactions when compound.

    ``legs`` is ``[(ChartOfAccounts | None, 'debit'|'credit', Decimal)]``.
    """
    dr_types, cr_types, dr_nums, cr_nums = _sides(legs)
    if not dr_types or not cr_types:
        return _empty_result()

    winners = _best_matches(dr_types, cr_types, dr_nums, cr_nums)
    if winners:
        out = winners[0].as_dict()
        out['confidence'] = 'exact'
        out['alternatives'] = [r.as_dict() for r in winners[1:]]
        return out

    # Compound entry: split it into one type pair at a time and match each pair
    # against the full catalog, band rules included — restricted to the accounts
    # actually carrying those types, so "Dr Kas / Cr Pendapatan" is still
    # recognised as a sale when it is only one half of a larger entry.
    # Iteration is over sorted types so the result does not depend on set order.
    components, seen = [], set()
    for dt in sorted(dr_types):
        for ct in sorted(cr_types):
            sub_dr = {a.account_number for a, side, amt in legs
                      if a is not None and amt and side == 'debit' and a.account_type == dt}
            sub_cr = {a.account_number for a, side, amt in legs
                      if a is not None and amt and side == 'credit' and a.account_type == ct}
            sub = _best_matches({dt}, {ct}, sub_dr, sub_cr)
            if sub and sub[0].code not in seen:
                components.append((dt, ct, sub[0]))
                seen.add(sub[0].code)
    if not components:
        return _empty_result()

    dr_totals, cr_totals = _type_totals(legs)
    top_dr = max(dr_totals, key=dr_totals.get) if dr_totals else None
    top_cr = max(cr_totals, key=cr_totals.get) if cr_totals else None

    headline = next(
        (rule for dt, ct, rule in components if dt == top_dr and ct == top_cr),
        components[0][2],
    )
    out = headline.as_dict()
    out['confidence'] = 'compound'
    out['alternatives'] = [rule.as_dict() for _dt, _ct, rule in components
                           if rule.code != headline.code]
    return out


# ── Balancing ─────────────────────────────────────────────────────────────────

def balance_hint(legs):
    """What it would take to balance ``legs``.

    ``needs_side`` is the side a new line must sit on to close the gap, and
    ``needs_amount`` is how much — which is what lets the composer fill the
    second line in for the operator instead of making them work out the
    direction themselves.
    """
    total_debit = sum((a for _acc, side, a in legs if side == 'debit' and a), Decimal('0'))
    total_credit = sum((a for _acc, side, a in legs if side == 'credit' and a), Decimal('0'))
    diff = total_debit - total_credit
    return {
        'total_debit': total_debit,
        'total_credit': total_credit,
        'difference': diff,
        'needs_side': 'credit' if diff > 0 else ('debit' if diff < 0 else ''),
        'needs_amount': abs(diff),
    }


# ── Posting window ────────────────────────────────────────────────────────────

def last_run_date():
    """The most recent date a journal run has posted, or None if never run.

    Deliberately the *maximum* posted date rather than "is this specific date
    posted". A day an earlier sweep skipped still sits before the last run, and
    letting a manual entry land there would change a period the operator has
    already reviewed and reported on. Corrections to anything at or before this
    date go through a correction journal, which reverses in the open period
    instead of rewriting a closed one.
    """
    return (
        JournalDayLog.objects
        .filter(is_posted=True)
        .order_by('-date')
        .values_list('date', flat=True)
        .first()
    )


def posting_window(today):
    """The dates a manual entry may carry, as ``(earliest, latest, blocked)``.

    ``earliest`` is the day after the last journal run — None when no run has
    ever happened, since nothing is closed yet. ``latest`` is ``today``: an entry
    dated forward would sit in a period the next journal run has not reached and
    would land in reports for a day that has not happened.

    ``blocked`` is True when the run has already covered today, leaving no legal
    date at all.
    """
    last = last_run_date()
    earliest = (last + timedelta(days=1)) if last else None
    blocked = bool(earliest and earliest > today)
    return earliest, today, blocked


BLOCKED_REASON = (
    'Jurnal sudah diposting sampai hari ini, sehingga tidak ada tanggal yang '
    'bisa dipakai untuk entri manual. Untuk memperbaiki periode yang sudah '
    'diposting, gunakan Jurnal Koreksi pada entri terkait.'
)


def window_payload(today):
    """The window as JSON, so the composer can bound its date input."""
    last = last_run_date()
    earliest, latest, blocked = posting_window(today)
    return {
        'last_run_date': last.isoformat() if last else None,
        'earliest': earliest.isoformat() if earliest else None,
        'latest': latest.isoformat(),
        'blocked': blocked,
        'reason': BLOCKED_REASON if blocked else '',
    }


def validate_date(entry_date, today):
    """None when ``entry_date`` may be posted, else an Indonesian error string."""
    earliest, latest, blocked = posting_window(today)
    if blocked:
        return BLOCKED_REASON
    if entry_date > latest:
        return f'Tanggal tidak boleh melewati hari ini ({latest.isoformat()}).'
    if earliest and entry_date < earliest:
        return (
            f'Tanggal {entry_date.isoformat()} termasuk periode yang sudah '
            f'diposting (jurnal terakhir dijalankan sampai '
            f'{(earliest - timedelta(days=1)).isoformat()}). Entri manual hanya '
            f'bisa dibuat mulai {earliest.isoformat()}. Untuk memperbaiki periode '
            f'yang sudah diposting, gunakan Jurnal Koreksi.'
        )
    return None


# ── Line parsing ──────────────────────────────────────────────────────────────

def resolve_accounts(lines):
    """Attach the ChartOfAccounts row to each parsed line.

    Returns ``(resolved, error)``. Head accounts are refused: they are the rollup
    rows the COA tree displays, and posting to one puts an amount in a subtotal
    that no sub-account explains.
    """
    ids = {l.get('account') for l in lines if l.get('account')}
    accounts = {a.id: a for a in ChartOfAccounts.objects.filter(id__in=ids)}
    resolved = []
    for idx, line in enumerate(lines, start=1):
        account = accounts.get(line.get('account'))
        if account is None:
            return None, f'Baris {idx}: rekening tidak ditemukan.'
        if account.is_head:
            return None, (
                f'Baris {idx}: {account.account_number} — {account.name} adalah '
                f'akun induk dan tidak bisa dijurnal. Pilih sub-akun di bawahnya.'
            )
        resolved.append({**line, 'account_obj': account})
    return resolved, None
