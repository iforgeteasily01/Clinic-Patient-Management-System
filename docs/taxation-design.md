# Taxation — design notes

`/accounting/tax` computes Indonesian tax figures from the `LedgerEntry` journal
using rules the operator builds on the page. **Nothing here posts to the ledger.**
A computed tax is a report; posting one would need the preview → review → commit
discipline the journal run already has, and that is deliberately not built yet.

## Why the rule is data

Rates, thresholds and the accounts that form a base all change — by statute, by
PKP status, by which regime the clinic elects. Hardcoding any of them means a
deployment every time the law moves. So a `TaxRule` row *is* the formula:

```
base   = Σ (component.sign × component.value)   ← TaxRuleComponent rows
base  -= deduction_amount                        ← floored at zero
result = round_down(apply_rate(base))            ← per rate_mode
```

There is no expression language. The page is a form, and the model carries the
shapes that a form can drive.

## The four rate modes

A single `rate_percent` field cannot express Indonesian rates, so `rate_mode`
selects the shape:

| Mode | Shape | Used by |
|---|---|---|
| `flat` | `base × rate` | PPN, PPh Final UMKM |
| `bracket` | progressive layers, each taxing only its own slice | PPh 21 |
| `facility` | the Pasal 31E turnover-proportional discount | PPh Badan |
| `none` | the base *is* the result | netting and intermediate figures |

`none` is what makes rule-to-rule arithmetic work without an expression parser:
*PPN Kurang Bayar* is a `none` rule with two components, `+ppn_keluaran` and
`−ppn_masukan`.

### Pasal 31E is its own mode, on purpose

31E is not "22% if turnover is low, else 22%". Income attributable to the first
Rp 4,8bn of turnover is taxed at half rate, and in the middle band that share is
*proportional*:

```
turnover ≤ 4,8bn           →  whole base facilitated
4,8bn < turnover ≤ 50bn    →  facilitated = base × 4,8bn / turnover
turnover > 50bn            →  no facility
```

Both caps and the 0.5 factor are fields, so the numbers move without a code
change. The turnover tested comes from **another rule's base**, not this rule's
own — 31E tests *peredaran bruto* while taxing *penghasilan kena pajak*.

## Two invariants worth not breaking

**Every figure comes from `account_movements()`** — the same helper the financial
reports use, reading `LedgerEntry` with `signed_balance()` applied. Cached
`ChartOfAccounts.balance` is never read. This is why a tax base and the Laba Rugi
it should agree with cannot diverge. Verified against the live ledger: the
`laba_kena_pajak` base matches `ProfitLossView._compute()`'s `net_profit` exactly.

**The compute endpoint sits behind `_unposted_gate()`.** A tax computed over a
period the journal run has not swept reads low and looks authoritative doing it.
Same gate, same reason, as every financial report.

## Rules are a graph

`source='rule'` components and `facility_turnover_rule` make the rule set a
directed graph. `order_rules()` evaluates in dependency order with cycle
detection — display order decides only what the page shows, never what is
computed first. A cycle raises `TaxRuleError` naming the loop rather than
recursing forever.

## Rounding

Indonesian tax figures round **down**, never half-up, so a rounded figure is
never more than the ledger supports. `thousand` rounds the magnitude and
restores the sign, so a lebih-bayar of −1.500 becomes −1.000, not −2.000.

## Seeded rules

`python manage.py seed_tax_rules` creates eight starting rules. Two need a human
decision before they mean anything, and say so in their own `notes`:

- **`ppn_keluaran`** — health services are largely VAT-exempt while retail product
  sales are not, so the seeded base is product revenue (4200000) only. If the
  clinic is not a PKP, deactivate the PPN rules entirely.
- **`ppn_masukan`** — input VAT comes off supplier tax invoices, which the ledger
  does not model. Seeded with **no base at all**: it computes zero and the page
  flags it as unconfigured rather than inventing a plausible-looking number.

`pph_21` is likewise an approximation — aggregate salary accounts against PTKP
TK/0, where the real calculation is per employee at their own PTKP status.

`pph_final_umkm` and `pph_badan` are mutually exclusive regimes. Both are seeded
active; the operator deactivates whichever does not apply.

## Files

| Path | Role |
|---|---|
| `managementsys/models.py` | `TaxRule`, `TaxRuleComponent`, `TaxRuleBracket` (migration 0108) |
| `managementsys/services/tax_engine.py` | evaluation, ordering, rounding |
| `managementsys/views/tax_page.py` | CRUD + compute endpoints |
| `managementsys/management/commands/seed_tax_rules.py` | the eight starting rules |
| `managementsys/tests/test_tax_engine.py` | 25 tests, all hand-computed figures |
| `CPMS-Webapp/src/pages/accounting/TaxPage.tsx` | results + trace |
| `CPMS-Webapp/src/pages/accounting/TaxRuleEditor.tsx` | the guided builder |

## Not built

- No GL posting. Deliberate — see the top of this file.
- No Excel export. The financial reports have one; this page does not yet.
- No fiscal corrections (koreksi fiskal) as a first-class concept — a `fixed`
  component is the current workaround for adding one to `laba_kena_pajak`.
- `tax_engine.py` imports `financial_reports_utils` lazily inside `tax_page.py`
  to avoid a package-init import cycle. The real fix is moving that module out
  of `views/`, which would touch every report.
