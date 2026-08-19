"""Placeholder substitution for CRM WhatsApp templates.

The operator writes ``Halo {nama}, ...`` and copies the rendered text into
WhatsApp by hand. Nothing here sends anything.

Two rules the callers depend on:

* **An unknown token is left alone.** ``{nama_belakang}`` stays literally
  ``{nama_belakang}`` in the output rather than becoming an empty string, so a
  typo is visible in the preview instead of silently deleting a word from a
  message that goes to a real patient.
* **A known token with no data renders its fallback**, not an empty gap —
  a patient with no ``birth_date`` gets ``{umur}`` → ``-``, which reads as
  missing data rather than as a broken sentence.

Dates are formatted in Indonesian and in Jakarta local time, because the only
consumer is a message written to a patient in Jakarta.
"""
from __future__ import annotations

import datetime

from ..models import JAKARTA_TZ

# Token → (label shown in the UI legend, one-line description). The CRM page
# renders this dict, so the legend cannot drift from what actually substitutes.
PLACEHOLDERS = {
    'nama':             'Nama pasien',
    'nama_panggilan':   'Nama depan pasien',
    'no_pasien':        'Nomor pasien',
    'telepon':          'Nomor telepon',
    'umur':             'Umur dalam tahun',
    'ulang_tahun':      'Tanggal lahir (mis. 14 Maret)',
    'kunjungan_terakhir': 'Tanggal kunjungan terakhir',
    'hari_sejak_kunjungan': 'Jumlah hari sejak kunjungan terakhir',
    'treatment_terakhir': 'Treatment pada kunjungan terakhir',
    'tier':             'Tier loyalitas',
    'total_kunjungan':  'Jumlah kunjungan',
    'tanggal_hari_ini': 'Tanggal hari ini',
}

_MONTHS_ID = [
    '', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
    'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember',
]

MISSING = '-'


def format_date_id(d: datetime.date | None, *, with_year: bool = True) -> str:
    if d is None:
        return MISSING
    out = f'{d.day} {_MONTHS_ID[d.month]}'
    return f'{out} {d.year}' if with_year else out


def render(body: str, context: dict[str, str]) -> str:
    """Substitute ``{token}`` for every key in ``context``; leave the rest.

    Implemented as a per-key replace rather than ``str.format`` on purpose:
    ``format`` raises KeyError on an unknown token and mangles a literal brace,
    and a template body is operator-written text where both are likely.
    """
    out = body
    for key, value in context.items():
        out = out.replace('{' + key + '}', value)
    return out


def build_context(patient, *, crm=None, last_visit=None, last_treatments=(), today=None) -> dict[str, str]:
    """Context for one patient. ``last_visit`` is a date, not a datetime.

    ``crm`` is the ``PatientCRMProfile`` when the caller already has it, so this
    does not re-query per patient in a list render.
    """
    today = today or datetime.datetime.now(JAKARTA_TZ).date()
    name = (patient.name or '').strip()
    age = patient.age_on(today)
    days_since = (today - last_visit).days if last_visit else None
    return {
        'nama':                 name or MISSING,
        'nama_panggilan':       name.split(' ')[0] if name else MISSING,
        'no_pasien':            patient.patient_no,
        'telepon':              (patient.phone_number or '').strip() or MISSING,
        'umur':                 str(age) if age is not None else MISSING,
        'ulang_tahun':          format_date_id(patient.birth_date, with_year=False),
        'kunjungan_terakhir':   format_date_id(last_visit),
        'hari_sejak_kunjungan': str(days_since) if days_since is not None else MISSING,
        'treatment_terakhir':   ', '.join(last_treatments) or MISSING,
        'tier':                 (crm.tier.name if (crm and crm.tier_id) else MISSING),
        'total_kunjungan':      str(crm.total_visits if crm else 0),
        'tanggal_hari_ini':     format_date_id(today),
    }
