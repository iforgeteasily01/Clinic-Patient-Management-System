# Clinic-Patient-Management-System — Agent Onboarding Guide

## Overview

Django 5.2 + DRF REST API backend for the CPMS clinic management system.
Serves the React web frontend (CPMS-Webapp) and the C# WPF cashier app (Medya-Cashier).

## Running

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver   # http://127.0.0.1:8000
```

Requires a `.env` file in the project root:
```
DB_NAME=cpms
DB_USER=postgres
DB_PASSWORD=...
DB_HOST=localhost
DB_PORT=5432
# Legacy iPos external DB (inventory sync)
EXT_DB_NAME=ipos
EXT_DB_USER=...
EXT_DB_PASSWORD=...
EXT_DB_HOST=...
EXT_DB_PORT=...
# Vercel dashboard push (optional)
CPMS_VERCEL_URL=https://cpms-dashboard-api.vercel.app
CPMS_INGEST_SECRET=...
```

## Tech Stack

| | |
|---|---|
| Framework | Django 5.2.7 |
| API | Django REST Framework 3.16.1 |
| Database | PostgreSQL (psycopg2-binary) |
| Auth | Custom token-based (Bearer) |
| Excel I/O | openpyxl 3.1.5 |
| Images | Pillow 12.2.0 |
| Env vars | environs / python-dotenv |

---

## Project Structure

```
Clinic-Patient-Management-System/
├── CPMS/
│   ├── settings.py          # Core config, dual-DB, CORS, DRF auth
│   ├── urls.py              # Root router → delegates to managementsys/urls.py
│   ├── wsgi.py / asgi.py
├── managementsys/
│   ├── models.py            # All 30+ models in one file
│   ├── urls.py              # 143 API routes
│   ├── auth_backend.py      # Custom token auth
│   ├── signals.py           # Post-save hooks → Vercel dashboard push
│   ├── vercel_push.py       # HTTP client for Vercel push
│   ├── apps.py              # Registers signals in ready()
│   ├── api/
│   │   └── serializers.py   # 50+ serializers
│   ├── views/               # 25 view files, one per feature area
│   └── migrations/          # 45 migrations
├── manage.py
└── requirements.txt
```

---

## Authentication

**All endpoints require authentication** except `GET /api/auth/users/` and `POST /api/auth/login/`.

### How it works
- `AppUserAuthentication` reads `Authorization: Bearer <token>` from request headers
- Looks up `AppUser` by `auth_token` field
- Returns `(AppUser, token)` on success, `None` if no token (fails open — unauthenticated requests proceed but hit permission check)
- `IsAppAuthenticated` permission class rejects any non-`AppUser` request

### Token lifecycle
1. Client POSTs `{ user_id, pin }` to `/api/auth/login/`
2. Server validates PIN against bcrypt hash in `AppUser.pin_hash`
3. `AppUser.generate_token()` creates a 64-char hex token, saved to `AppUser.auth_token`
4. Token returned to client; included in all subsequent requests
5. Logout: `AppUser.clear_token()` sets `auth_token = ''`

### User Roles
| Role | Access |
|---|---|
| `superuser` | Full access to all endpoints |
| `doctor` | Clinical workflows (appointments, medical records) |
| `beautician` | Treatment queue, treatment sessions |
| `cashier` | Billing, invoices, POS |
| `manager` | CRM, HR, admin CRUD, reports |

---

## All Models (`managementsys/models.py`)

All models are in a single file. Search carefully.

### Core Clinical

| Model | Key Fields | Notes |
|---|---|---|
| `Patient` | `patient_no` (PK, e.g. J000001), `name`, `phone_number`, `NIK`, `updated_at` | patient_no auto-generated from first-name initial + counter |
| `ActivePatient` | `patient_no` (FK), `guest_name`, `status` (int), `consult_status` (bool), `visit_time`, `medrec` (OneToOne) | Tracks today's queue. Supports registered patients + walk-in guests |
| `MedRec` | `medrec_id` (PK, e.g. MR-J000001-20260601-1), `doctor_id` (FK), `patient_no` (FK), `subjective`, `objective`, `assessment`, `assessment_codes` (JSONField), `plan`, regimen fields (8 product fields) | SOAP record. medrec_id auto-generated |
| `Doctors` | `doctor_name` | Simple registry |
| `Beauticians` | `beautician_name`, `bphone_number`, `available` | Tracks availability |

### Treatment & Services

| Model | Key Fields | Notes |
|---|---|---|
| `Treatment` | `code` (unique), `name`, `category` (FK→TreatmentCategory), `price`, `active`, `catalog_item` (OneToOne→InventoryItem) | Auto-creates a mirror InventoryItem (is_service=True) on save. Custom queryset cascades delete to catalog_item |
| `TreatmentCategory` | `name`, `revenue_account` (OneToOne→COA), `cogs_account` (OneToOne→COA) | Auto-creates GL accounts on creation |
| `TreatmentSession` | `active_patient` (FK), `patient_no` (FK), `beautician` (FK), `treatments` (M2M), `session_time` | One record per beautician-led session |
| `TreatmentPackage` | `code`, `name`, `price`, `catalog_item` (OneToOne→InventoryItem) | Bundle of multiple treatments sold upfront |
| `TreatmentPackageItem` | `package` (FK), `treatment` (FK), `sessions` (int) | Which treatments and how many sessions each |

### Inventory & Stock

| Model | Key Fields | Notes |
|---|---|---|
| `InventoryItem` | `code` (unique), `name`, `selling_price`, `unit_small/medium/large`, `is_service`, `is_active`, `min_stock`, `item_category` (FK→TreatmentCategory) | Unified catalog: physical items + service mirrors |
| `Warehouse` | `code`, `name`, `is_active` | Physical or logical stock location |
| `InventoryBatch` | `item` (FK), `warehouse` (FK), `input_date`, `quantity_initial`, `quantity_remaining`, `value` | FIFO batch tracking. quantity_remaining decremented on stock-out |
| `StockOpnameSession` | `date`, `status` (draft/completed), `conducted_by`, `notes` | Physical inventory count session |
| `StockOpnameItem` | `session` (FK), `item` (FK), `warehouse` (FK), `shelf1_qty`, `shelf2_qty`, `system_qty`, `is_loss` | Line item for physical count vs. system quantity |

### Billing & Financial

| Model | Key Fields | Notes |
|---|---|---|
| `Invoice` | `invoice_number` (unique, auto INV-YYYYMMDD-N), `patient_no` (FK), `payment_method` (FK→COA asset), `discount`, `tax`, `additional_charges`, `grand_total`, `cashier` (FK), `warehouse` (FK), `promotion` (FK) | invoice_number auto-generated |
| `InvoiceItem` | `invoice` (FK cascade), `item` (FK), `item_name`, `quantity`, `price`, `discount_pct` | item_name allows null (custom items) |
| `InvoicePayment` | `invoice` (FK cascade, `related_name='payments'`), `payment_method` (FK), `payment_account` (FK→COA asset), `amount`, `sort_order` | Split payments only — one row per tender, must sum to `grand_total`. Empty for a single-method invoice, which is still described by `Invoice.payment_method`/`payment_account` alone. POST/PATCH `/api/invoices/` accept it as `payments: [{payment_method_id, amount}]` |
| `PatientPackage` | `patient` (FK), `package` (FK), `purchased_invoice` (FK), `status` (active/exhausted) | Purchase record for a treatment package. Methods: `remaining_for(treatment_id)`, `total_remaining()`, `refresh_status()` |
| `PatientPackageRedemption` | `patient_package` (FK cascade), `treatment` (FK), `invoice` (FK), `redeemed_at` | One per session that uses a package |
| `ChartOfAccounts` | `account_number` (unique), `name`, `account_type` (choice), `balance`, `is_head`, `parent` (self-FK) | GL accounts. Types: asset/liability/equity/revenue/cogs/expense/other_income/other_expense |

### CRM & Promotions

| Model | Key Fields | Notes |
|---|---|---|
| `PatientTier` | `name`, `min_visit_count`, `min_total_spend`, `color_hex`, `sort_order` | Loyalty tiers (Bronze, Silver, Gold…) |
| `PatientCRMProfile` | `patient_no` (OneToOne→Patient), `tier` (FK), `total_spend`, `total_visits`, `last_visit_date` | Customer lifetime value data |
| `Promotion` | `code` (unique), `discount_type` (percent/fixed), `discount_value`, `scope` (all/category/item), `min_tier` (FK), `is_auto`, `is_active`, `valid_from`, `valid_until` | `is_auto` = applies automatically. scope determines what items are eligible |
| `PromotionUsage` | `promotion` (FK protect), `patient_no` (FK), `invoice` (OneToOne), `discount_applied` | Audit trail per invoice |

### Staff & HR

| Model | Key Fields | Notes |
|---|---|---|
| `AppUser` | `display_name`, `pin_hash`, `role`, `avatar_color`, `auth_token`, `base_salary`, `profile_picture`, theme color fields | Methods: `set_pin()`, `check_pin()`, `generate_token()`, `clear_token()` |
| `WorkShift` | `name`, `expected_start`, `expected_end`, `color_hex` | |
| `StaffSchedule` | `staff` (FK), `date`, `shift` (FK) | Unique(staff, date) |
| `AttendanceRecord` | `staff` (FK), `date`, `clock_in`, `clock_out`, `status` (present/late/absent/half_day/day_off) | Properties: `total_hours`, `late_minutes` |

### Documentation & Config

| Model | Key Fields | Notes |
|---|---|---|
| `SoapTemplate` | `field` (subjective/objective/assessment/plan), `title`, `body`, `sort_order` | Snippet library for SOAP entries |
| `PatientNote` | `patient_no` (FK), `date`, `content`, `author` | Free-form notes |
| `PatientPhoto` | `patient_no` (FK), `photo_date`, `body_area`, `image` | Upload path: `patient_photos/YYYY/mm/dd/` |
| `AssessmentCode` | `code` (unique), `description`, `active`, `category` (1=Common, 2=Uncommon) | ICD-10 codes |
| `AuditLog` | `performed_by` (FK), `action`, `entity_type`, `entity_id`, `description` | Actions: LOGIN, LOGOUT, CREATE, UPDATE, DELETE, STATUS_CHANGE |
| `SiteConfig` | `clinic_name`, `address_line1/2`, `phone_fax`, `receipt_footer` | Singleton. Use `SiteConfig.get_solo()` |
| `IssueTicket` | `ticket_no` (auto TKT-YYYYMMDD-NNNN), `submitted_by` (FK), `category`, `title`, `status` | Statuses: open/in_progress/resolved/closed |

---

## API Routes (143 total)

### Authentication — `/api/auth/`
| Method | Path | View | Notes |
|---|---|---|---|
| GET | `/api/auth/users/` | UserListView | No auth needed — login screen |
| POST | `/api/auth/login/` | LoginView | Body: `{ user_id, pin }` → `{ token, user }` |
| POST | `/api/auth/logout/` | LogoutView | Clears token |
| GET/PATCH | `/api/auth/profile/` | ProfileUpdateView | Current user |
| PATCH | `/api/auth/profile/theme/` | ThemeUpdateView | Save theme colors |

### Patients — `/api/patients/` & `/api/activepatients/`
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/patients/` (legacy) | Patient list/create |
| POST | `/api/patients/new/` | Create patient + auto-checkin |
| GET | `/api/patients/search/` | Fuzzy search `?search=` |
| GET | `/api/patients/count/` | Total patient count |
| GET | `/api/patients/sync/` | Export for mobile sync |
| GET/POST | `/activepatient/` | Today's queue |
| PATCH | `/api/activepatients/update-status/` | Update patient status in queue |
| DELETE | `/api/activepatients/clear/` | Bulk clear queue |
| GET | `/api/patients/<patient_no>/packages/` | Active/exhausted packages |

### Appointments & Treatment Workflow
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/appointments/add/` | Add registered patient to queue |
| POST | `/api/appointments/general/` | Add walk-in guest to queue |
| GET | `/api/treatments/` | All active treatments |
| GET | `/api/activepatients/treatment/` | Treatment queue (status=3) |
| POST | `/api/treatment-session/` | Create treatment session |
| POST | `/api/treatment-session/complete/` | Mark treatment complete (→ status 5) |
| GET | `/beauticians/` | Beauticians (`?available=true`) |

### Medical Records
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/medicalrecord/` | Create SOAP record |
| GET | `/api/medicalrecords/<patient_no>/` | All records for patient |
| GET | `/api/medical-records/history/` | All records, filterable |
| GET | `/api/medical-records/history/<medrec_id>/` | Single record detail |
| POST | `/api/photos/upload/` | Upload patient photo |
| GET | `/api/photos/` | List photos `?patient_no=` |

### Billing & Invoices
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/billing/` | Patients ready for checkout (status=5) |
| POST | `/api/billing/<id>/complete/` | Generate invoice + mark paid |
| GET/POST | `/api/invoices/` | Invoice list / create |
| POST | `/api/invoices/create/` | Create with line items |
| GET | `/api/invoices/<id>/` | Invoice detail |
| GET | `/api/invoices/export/` | Download as Excel |
| POST | `/api/invoices/import/` | Bulk import from Excel |

### Inventory
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/inventory/items/` | Items (`?search=`, `?stock_only=true`) |
| GET | `/api/inventory/items/template/` | Download import template |
| POST | `/api/inventory/items/import/preview/` | Preview bulk import |
| POST | `/api/inventory/items/import/` | Confirm bulk import |
| GET/PUT | `/api/inventory/items/<id>/` | Item detail |
| GET/POST | `/api/inventory/warehouses/` | Warehouse CRUD |
| GET | `/api/inventory/stock/` | Current stock levels |
| GET | `/api/inventory/batches/` | All FIFO batches |
| POST | `/api/inventory/stock-in/` | Receive stock |
| POST | `/api/inventory/stock-out/` | Issue stock |
| GET | `/api/inventory/sync/items/` | Paginated export for mobile |
| GET/POST | `/api/stock-opname/` | Physical count sessions |
| GET/PUT | `/api/stock-opname/<id>/` | Session detail |
| POST | `/api/stock-opname/<id>/complete/` | Finalize count |

### SOAP Templates
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/soap-templates/` | List/create (`?field=subjective\|objective\|assessment\|plan`) |
| GET | `/api/soap-templates/sample/` | Download blank import template |
| POST | `/api/soap-templates/import/` | Bulk import from Excel |
| GET | `/api/soap-templates/export/` | Download all as Excel |
| GET/PUT/DELETE | `/api/soap-templates/<id>/` | Detail |

### Assessment Codes (ICD-10)
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/assessment-codes/` | List/create (`?search=`) |
| POST | `/api/assessment-codes/import/preview/` | Preview Excel import |
| POST | `/api/assessment-codes/import/confirm/` | Confirm import |
| GET/PUT/DELETE | `/api/assessment-codes/<id>/` | Detail |

### Promotions
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/promotions/` | List/create |
| POST | `/api/promotions/validate/` | Check promo eligibility |
| GET/PUT/DELETE | `/api/promotions/<id>/` | Detail |

### CRM
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/crm/patients/` | Patient CRM list (`?q=`, `?tier=`, `?page=`) |
| GET | `/api/crm/patients/<patient_no>/` | CRM detail |
| GET/POST | `/api/admin/tiers/` | Tier CRUD |
| GET/PUT/DELETE | `/api/admin/tiers/<id>/` | Tier detail |

### HR
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/hr/attendance/clock-in/` | Clock in |
| POST | `/api/hr/attendance/clock-out/` | Clock out |
| GET | `/api/hr/attendance/` | Records (`?staff_id=`, `?date_from=`, `?date_to=`, `?status=`) |
| GET | `/api/hr/attendance/summary/` | Summary (`?staff_id=`, `?month=`) |
| GET/POST | `/api/hr/shifts/` | Shift CRUD |
| GET/POST | `/api/hr/schedules/` | Staff schedule |
| GET | `/api/hr/performance/` | Aggregated performance |
| GET | `/api/hr/performance/daily/` | Per-day breakdown |
| GET | `/api/reports/dashboard/` | Dashboard analytics |

### Admin CRUD
All under `/api/admin/` — require `superuser` or `manager` role:
`doctors/`, `patients/`, `treatments/`, `treatment-packages/`, `beauticians/`, `users/`, `treatment-categories/`, `accounts/`, `site-config/`

### Tickets
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/tickets/` | List/create (`?status=`) |
| GET/PUT/DELETE | `/api/tickets/<id>/` | Detail |
| POST | `/api/tickets/<id>/images/` | Attach image |

---

## View Files (`managementsys/views/`)

| File | Endpoints contained |
|---|---|
| `auth_views.py` | Login, logout, user list, profile, theme |
| `patient_page.py` | Search, create+checkin, queue, appointment add, treatment session, status update, sync |
| `medical_record_page.py` | MedRec by patient |
| `medical_record_history.py` | History list + detail |
| `photo_page.py` | Photo upload + list |
| `billing_page.py` | Billing queue, complete billing |
| `invoice_page.py` | Invoice CRUD, export, import |
| `inventory_page.py` | Item CRUD, warehouse, stock-in/out, batches, sync, import |
| `stock_opname_page.py` | Physical count sessions |
| `soap_templates_page.py` | Template CRUD, import, export |
| `patient_notes_page.py` | Notes CRUD |
| `assessment_codes_page.py` | ICD-10 CRUD, two-phase import |
| `promotion_page.py` | Promotion CRUD, validation |
| `crm_page.py` | CRM list/detail, tier CRUD |
| `hr_attendance_page.py` | Clock-in/out, records, summary, shifts, schedules |
| `hr_performance_page.py` | Performance + daily |
| `package_page.py` | Patient packages |
| `reports_page.py` | Dashboard |
| `tickets_page.py` | Tickets + images |
| `admin_views.py` | Admin CRUD for all entities |

---

## Serializers (`managementsys/api/serializers.py`)

50+ serializers in one file. Key patterns:
- `SerializerMethodField` extensively used for computed properties (names, URLs, totals)
- `request.build_absolute_uri()` used for all image/file URL fields — requires `context={'request': request}`
- Nested serializers for related objects (e.g., TreatmentPackageItem inside TreatmentPackage)
- Two-tier serializers: `*ListSerializer` (lighter, for lists) + `*DetailSerializer` (full data)
- `InvoiceCreateSerializer` validates at least one line item is provided
- `AppUserAdminSerializer` exposes write-only `pin` field, validates 6 digits exactly
- `ChartOfAccountsSerializer` validates account_number falls in correct range per account_type

---

## Key Business Logic

### Patient Flow (ActivePatient status codes)
| Status | Meaning |
|---|---|
| 1 | Waiting / Checked in |
| 2 | With doctor (consult) |
| 3 | Treatment queue (ready for beautician) |
| 4 | In treatment |
| 5 | Ready for billing |

### Auto-Generated IDs
- `patient_no`: `{initial}{6-digit-counter}` e.g. J000001
- `medrec_id`: `MR-{patient_no}-{YYYYMMDD}-{N}` e.g. MR-J000001-20260601-1
- `invoice_number`: `INV-{YYYYMMDD}-{N}` e.g. INV-20260601-3
- `ticket_no`: `TKT-{YYYYMMDD}-{NNNN}`

### Treatment ↔ InventoryItem Mirror
When a `Treatment` or `TreatmentPackage` is saved, a mirror `InventoryItem` (is_service=True) is auto-created/updated via `save()`. Use `TreatmentQuerySet.delete()` to ensure cascade deletion.

### FIFO Inventory
`InventoryBatch` tracks stock by batch. Stock-out deducts from oldest batch first (ordered by `input_date`).
Restock (`_fifo_restock`) refills **newest-first**, so reversing and re-posting the same line can shift COGS
between batches when an item has batches at differing unit costs. Exact reversal would need per-line batch tracking.

### Invoice Edit = Reverse + Re-post
`PUT/PATCH /api/invoices/<pk>/` replaces line items wholesale (any `item_id`, quantity, price, discount_pct).
It reverses everything the original posting did — payment, revenue, FIFO stock, COGS, treatment materials,
package sales/redemptions — then re-applies it for the new lines. Reversals are written as opposite-side
`LedgerEntry` rows, so both the original and the correction stay in the journal.
`DELETE` (void) reverses without re-posting.

Two constraints worth knowing:
- An edit is **refused** (400) when a treatment package sold by the invoice has been redeemed on a *different*
  invoice — reversing the sale would cascade-delete that redemption.
- `_post_accounting` and `_reverse_accounting_instances` must stay mirror images. If you add a side effect to
  one, add it to the other, or edits and voids will silently corrupt balances.
  Covered by `tests/test_invoice_edit.py` (create → edit → void must return every balance and batch to baseline).

### Excel Import Pattern (two-phase)
1. POST file to `import/preview/` → returns rows for user review
2. POST confirmed rows to `import/confirm/` → writes to database
Used by: assessment codes, inventory items, SOAP templates.

### Vercel Dashboard Push
Three signals fire HTTP POSTs to the Vercel dashboard on:
- `ActivePatient` created → `checkin` event
- `TreatmentSession` created → `in_treatment` event
- `Invoice` created → `invoice_completed` event

---

## Settings Notes

| Setting | Current value | Note |
|---|---|---|
| `DEBUG` | `True` | Must set to `False` in production |
| `ALLOWED_HOSTS` | `["*"]` | Restrict in production |
| `CORS_ALLOW_ALL_ORIGINS` | `True` | Restrict in production |
| `SECRET_KEY` | Hardcoded insecure | Use env var in production |
| `DEFAULT_DB` | PostgreSQL via `.env` | Defaults in settings are `cpms`/`postgres`/`seesaw` |
| `TIME_ZONE` | UTC | |

## Management Commands

```bash
python manage.py create_default_admin     # Create superuser (display_name=Admin, PIN=000000)
python manage.py create_app_user <name> <pin> [--role admin]
python manage.py populate_data            # Load demo data
```

## Migrations

45 migrations. Two merge migrations (0039, 0040) resolved branching conflicts. Notable seed data in `0013_chartofaccounts_seed.py`. Always run `migrate` after pulling.
