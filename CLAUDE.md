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
# Vercel dashboard push + online reservation pull (optional)
CPMS_VERCEL_URL=https://cpms-dashboard-api.vercel.app
CPMS_INGEST_SECRET=...
# Length of an imported online booking. Keep equal to slot_minutes on the
# Vercel admin page or the schedule shows gaps and overlaps that do not exist.
CPMS_RESERVATION_DURATION_MINUTES=30
# Branch an online booking is filed under. Unset = null (visible everywhere).
CPMS_RESERVATION_BRANCH_ID=
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

### Online Reservations
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/reservations/inbox/` | Worklist (`?filter=attention\|unmatched\|all`) + summary counts |
| POST | `/api/reservations/sync-now/` | Collect one batch on demand. 503 when the link to Vercel is down |
| POST | `/api/reservations/<pk>/link-patient/` | Attach the patient the phone could not resolve to |
| POST | `/api/reservations/<pk>/acknowledge/` | "Seen" — moves the row out of the default filter |

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
| GET | `/api/inventory/dashboard/` | Low-stock counts + alert rows for the module hub |
| POST | `/api/inventory/stock-in/` | Receive stock — **always zero-value**, ignores any `value` sent |
| POST | `/api/inventory/stock-out/` | Issue stock (FIFO); `reason` picks the GL account |
| GET | `/api/inventory/stock-out/reasons/` | Reason → account catalog, with live COA names |
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
Two things behind one prefix. `crm_page.py` is the **patient directory** (the
`/patients` page); `crm_dashboard.py` is the **relationship view** (the `/crm`
page). Both read the same `PatientCRMProfile`, so visit counts can't disagree.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/crm/patients/` | Patient CRM list (`?q=`, `?tier=`, `?page=`) |
| GET | `/api/crm/patients/<patient_no>/` | CRM detail |
| GET | `/api/crm/dashboard/` | 30-day summary + deltas, activity buckets, demographics, top treatments/products, upcoming birthdays, and the recent-visitor list. `?recent_days=` (1-90, default 7) widens **only** that list — the 30-day summary window is fixed so the headline figures stay comparable. Each recent row carries `days_ago`, computed in Jakarta, which drives the recency colours on `/crm/activity` |
| GET | `/api/crm/patients/<patient_no>/profile/` | Relationship profile — visits collapsed to treatments + products, favourites, birthday, message context |
| GET/POST | `/api/crm/message-templates/` | WhatsApp template CRUD (+ `placeholders` legend) |
| GET/PUT/DELETE | `/api/crm/message-templates/<id>/` | Template detail |
| POST | `/api/crm/message-templates/<id>/render/` | Fill `{placeholder}` tokens for one patient |
| GET/POST | `/api/admin/tiers/` | Tier CRUD |
| GET/PUT/DELETE | `/api/admin/tiers/<id>/` | Tier detail |

Three constraints in `crm_dashboard.py` worth reading before editing it:
- **Days are cut in `Asia/Jakarta`**, not `timezone.now().date()` — this is
  clinic-facing, so a 22:30 WIB checkout belongs to that day.
- **A visit is a non-voided invoice.** Patients still in the queue have none
  yet, so today's list unions in open `ActivePatient` rows flagged `in_clinic`.
- **`/api/crm/dashboard/` must stay declared before `/api/crm/patients/<str:patient_no>/`**
  in `urls.py` or 'dashboard' is read as a patient number.

A template is text with `{token}` placeholders substituted by
`services/message_templates.py`; an unknown token is left intact rather than
blanked, so a typo is visible in the preview instead of silently deleting a word.
Copying a rendered template by hand needs no consent flag — only the **blast**
path below does.

### WhatsApp (OpenWA gateway)

The gateway is **a separate Node service**, not part of Django: OpenWA
(github.com/rmyndharis/OpenWA), default port 2785, `X-API-Key` auth. Django
holds the key, decides who gets a message, and records what was sent. It never
speaks to WhatsApp itself. `services/whatsapp_gateway.py` carries the full route
map and is dependency-free `urllib`, so a gateway that is down or misconfigured
can never break `manage.py migrate`.

| Method | Path | Purpose |
|---|---|---|
| GET/PUT | `/api/whatsapp/settings/` | Gateway URL, key, pacing, guard rails. The key is **never** returned — only `api_key_set`/`api_key_hint` |
| GET | `/api/whatsapp/status/` | Gateway health + session state. Never raises; unreachable is a normal pre-setup state |
| POST | `/api/whatsapp/session/<action>/` | `create` \| `start` \| `stop` \| `logout` \| `qr` |
| POST | `/api/whatsapp/test-message/` | One message to a typed number. Skips opt-in by design — it is not a patient |
| GET | `/api/whatsapp/segments/` | Audience catalog with live eligible counts |
| POST | `/api/whatsapp/blasts/preview/` | Resolve audience + render a sample. Writes nothing |
| GET/POST | `/api/whatsapp/blasts/` | History / send |
| GET | `/api/whatsapp/blasts/<id>/` | **Syncs from the gateway, then returns** — see below |
| POST | `/api/whatsapp/blasts/<id>/cancel/` | Stop the remaining messages |
| PATCH | `/api/patients/<no>/wa-opt-in/` | Consent toggle. Audit-logged |

**A blast has no background worker.** OpenWA's `send-bulk` is asynchronous — it
takes ≤100 messages, returns a `batchId`, and delivers them with a delay. So
POSTing a blast writes every recipient row and dispatches the first chunk; the
**detail endpoint is what advances it**, reconciling the current batch and
dispatching the next chunk when it drains. The page polls, so a blast survives a
Django restart: all state is in the two tables plus the gateway. Do not add
Celery for this.

**Four guard rails, all server-side and none overridable from the UI:**
- `Patient.wa_opt_in` — False for everyone by default, and deliberately *not*
  backfilled. Consent the patient never gave is not consent.
- a usable Indonesian mobile (`normalize_phone`); a landline or typo is dropped,
  never guessed at
- `per_patient_cooldown_days` — overlapping segments are normal, two messages in
  one day is how a number gets reported
- `daily_send_cap` and Jakarta-local quiet hours

`services/wa_audience.py` owns the segments and reports the whole funnel
(`matched → opted_in → unusable_number → in_cooldown → eligible`) so the UI can
explain why a 1.115-patient segment sends 6 messages.

⚠️ OpenWA is an **unofficial** gateway built on reverse-engineered clients. Its
own README states it is not approved for regulated-compliance use (healthcare
included) and carries a non-zero risk of WhatsApp restricting the account. The
conservative pacing defaults exist because of that.

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
| `reservations_inbox.py` | Online reservation worklist, patient linking, manual sync |

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

### Inventory ↔ GL boundary
Only **one door** puts value into inventory: `PurchaseInvoice`. It creates batches
directly (`views/accounting_page.py`), sets `value`, stamps `purchase_invoice`, and
posts Dr Persediaan / Kr Hutang-vendor.

`StockInView` is the other way in and it is **quantity-only** — it ignores any
`value` in the payload and writes `value=0`. It posts no journal, so a value here
would inflate the balance sheet with no credit and then surface later as FIFO COGS
on the way out. A zero-value batch costs nothing to issue, which is correct: the
clinic never recorded paying for it.

`StockOutView` captures the FIFO cost at deduction time (it cannot be recomputed —
the batches have moved on) and stores the operator's `reason`.
`StockOutLog.REASON_ACCOUNTS` maps reason → account; `_stock_out_posting_status`
marks a row `posted` up front when it has nothing to journal (a transfer, or a
zero-cost draw) so the sweep stops reconsidering it every run.

`InventoryDashboardView` classifies stock **per item across warehouses**, not per
warehouse row — `min_stock` belongs to the item. `LOW_STOCK_WARN_RATIO = 1.2`
defines the amber band and is mirrored in the frontend's `inventoryTypes.ts`.

Covered by `tests/test_inventory_module.py`.

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

## Online Reservations

Bookings made on the public web form (`cpms-dashboard-api/public/reserve.html`)
become ordinary `Appointment` rows in the clinic's own database.

### The clinic collects; Vercel never pushes

The dashboard push in `vercel_push.py` works because Vercel is on the public
internet. The reverse is not true — this backend sits on a clinic LAN with no
public address — so reservations travel by **poll**:

```
manage.py poll_reservations --loop --interval 60
    GET  /api/reservation-sync    every row with pulled_at IS NULL
    write Appointment + ReservationRequest, per row, in its own transaction
    POST /api/reservation-sync    ack only what committed
```

**A row is acked only after its write commits.** Vercel keeps re-delivering
anything unacked, so a crash mid-batch costs a redelivery, never a booking —
and `ReservationRequest.external_id` is unique, which is what makes a
redelivery harmless. One malformed row is skipped and left unacked rather than
stranding the rest of the batch behind it.

The poller is a **separate window** in `start-servers.bat`, not a thread in
`apps.ready()`: closing it loses nothing, and one pass is idempotent, so
running the one-shot form by hand is the fastest way to test the link.

### The appointment is created on arrival, not on approval

The public form already enforces opening hours, slot capacity, the booking
window and its own rate limits before it accepts anything. By the time a row
reaches here **the slot is taken** — the clinic promised it. Holding bookings in
a pending tray would let reception double-book a time a patient already has, so
`/reservations` is a *worklist*, not a gate.

Imported appointments carry `source='online'` (read-only in the serializer — a
staff booking must not be able to pose as a web one) and `contact_phone`. The
schedule page paints them with `--accent-online` and an "Online" badge; origin
takes the left edge from status because status is readable from the badge on
the right and origin is not readable anywhere else.

**Check-in is not duplicated.** An online booking enters the queue through
`AppointmentCheckInView` like every other appointment — one door, one code path.

### Phone matching refuses to guess

`Patient.phone_number` is free text (`08123456789`, `0812-3456-789`,
`+628123456789` all appear), so no SQL predicate matches all of it. Both sides
are normalised through `whatsapp_gateway.normalize_phone` in Python, over a scan
of two small columns — a few milliseconds once a minute, and exact where a LIKE
would be a guess. Four outcomes, and only the first is automatic:

| `match_status` | Meaning | What happens |
|---|---|---|
| `matched` | Exactly one patient | Appointment is filed against that chart |
| `unmatched` | No patient has that number | Booked as a guest, phone kept on the appointment |
| `ambiguous` | Two or more patients | **Never resolved** — candidates handed to reception |
| `invalid_phone` | Not a readable Indonesian mobile | Guest, flagged for a human |

Two hits are never collapsed into one, for the same reason the bank reconciler
refuses an ambiguous match: a wrong link writes a stranger's visit into
somebody's medical record. `ReservationLinkPatientView` is how a human resolves
it, and it is **refused once the booking is checked in** — `ActivePatient` and
anything hanging off it were created against whoever the row said it was, and
re-pointing the appointment afterwards leaves the queue and the schedule
disagreeing about who is in the building.

### Vercel side

`api/reservation-sync.js` (staff secret, no origin allowlist — the caller is a
server with no `Origin` header). `reservations.pulled_at` / `clinic_ref` track
collection; the ack is idempotent via `WHERE pulled_at IS NULL`.

Covered by `tests/test_online_reservations.py`.

---

## Sales Returns

A return is a **separate document**, never an edit of the invoice. Using
`PUT /api/invoices/<pk>/` for a return would rewrite what the books said
happened on the sale date, erase the fact that goods came back at all, and leave
the CRM believing the patient simply bought less. `Invoice.is_voided` stays
False: the sale happened.

`SalesReturn` + `SalesReturnItem`, driven by `services/sales_returns.py`.

### Lifecycle — identical to an invoice

Created `unposted` with **zero ledger rows and no stock movement**. The journal
run posts it, and the run is also where `_fifo_restock` actually happens: the
COGS the entry needs cannot be known without doing the restock, exactly as the
invoice's FIFO cost cannot be known without doing the deduction. Registered as
the `'sales_return'` kind in `journal_sweep._gather_events`,
`journal_preview` (fingerprint / `build_legs_for` / `SOURCE_TYPE_BY_KIND` /
`MODEL_BY_KIND` / `document_label`) and `accounting_page._RUN_POSTERS`.

### The entry — the invoice's mirror

```
Dr  revenue per line     gross (price x qty, via _line_revenue_account)
Dr  Tax Payable          apportioned
Dr  Additional Charges   apportioned
    Cr  refund account   total_refund
    Cr  Sales Discount   the plug — discount being un-granted
```
plus, per restocked physical line: `Dr Inventory / Cr COGS` at the FIFO cost.

Revenue routes through the invoice's own `_line_revenue_account`, so a return
credits back exactly the account the sale debited.

### The refund is computed, never typed

`compute_refund` apportions the invoice-level discount, tax and charges **once,
against the returned subset as a whole**, in proportion to *net* line value (not
gross — two lines of equal list price where one carried a 50% line discount did
not contribute equally). A restocking fee or partial goodwill refund is a
separate expense/other-income entry; folding it into the total would misstate
revenue.

### Rules worth knowing

- **No edit.** Void and re-enter. A return is a physical event; amending one
  means the clinic is unsure what came through the door.
- **Voiding a posted return** deducts the stock again *and* writes a reversal
  memo via `reverse_legset(legset_from_entry(...))` — never by rebuilding the
  legs, which would re-run FIFO against today's batches.
- **A redeemed treatment package blocks the line** (`_package_block_reason`),
  mirroring the same refusal in the invoice-edit path.
- **A service is never restocked**, whatever the client sends.
- **Branch comes from the invoice**, not the request header: a refund booked
  into a different branch than the revenue it reverses leaves both wrong.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/invoices/<pk>/returnable/` | Per-line open quantities + block reasons |
| POST | `/api/returns/preview/` | What a proposed return would refund. Writes nothing |
| GET/POST | `/api/returns/` | List / create |
| GET/DELETE | `/api/returns/<pk>/` | Detail / void |

Covered by `tests/test_sales_returns.py` — the load-bearing test is that a full
return of an entire invoice returns every balance and every batch to baseline.

---

## Bank Reconciliation

`BankReconciliation` + `BankStatementLine`, driven by
`services/bank_reconciliation.py` and `services/statement_import.py`.

**Nothing here writes to the ledger.** A reconciliation is an assertion *about*
the books, never a change to them: a bank charge nobody recorded is fixed by
entering an expense, not by the reconciler inventing a journal line. If that
rule bends, reconciliation stops being evidence and becomes a second, unreviewed
posting path.

### Signs are normalised once, at the edge

A `LedgerEntry` on an asset account is a debit (in) or credit (out); a statement
is two columns whose names differ per bank. Both become one signed number —
**positive in, negative out** — via `signed_amount` and the import. Every
comparison downstream is plain arithmetic.

### Auto-matching refuses ambiguity

Two passes: same-date-same-amount, then amount within `AUTO_MATCH_WINDOW_DAYS`
(5). A line with more than one candidate is **left unmatched**, and a candidate
claimed earlier in the run is off the table. A wrong match balances the period
and hides a real discrepancy; an unmatched line is visible and gets dealt with.

The rule is deliberately **asymmetric**. One statement line with two candidate
book rows is refused — those rows are distinct records ("Setoran A" vs "Setoran
B") and picking one misattributes the clearing. Two identical statement lines
with one book row is *not* the same case: the lines carry no information that
distinguishes them, so one is matched and the other left over, which states the
discrepancy ("one Rp 500.000 deposit is missing from the books") in one line
instead of three loose ends.
`match_line` lets an operator force what the matcher refused, including a
different amount (a netted bank fee is real) — the summary surfaces the gap
rather than hiding it.

### Two different numbers, reported separately

- `difference` — ledger balance at period end minus the statement's closing
  balance. **The headline.** Explained by the two enumerable lists: statement
  lines with no book entry, and book entries with no statement line.
- `statement_drift` — whether the *imported lines* add up from the stated
  opening balance to the stated closing balance. Non-zero means the import is
  incomplete, and chasing `difference` before fixing it is wasted effort.

### Completing clears

`complete()` is refused unless `difference == 0` **and** nothing is unmatched on
either side — a closed reconciliation that does not balance looks finished, so
nobody returns to it. It stamps `LedgerEntry.reconciliation`, which is what
makes the next period correct: a cleared transaction is never offered again.
`reopen()` releases the stamps but keeps the matches.

Overlapping periods on one account+branch are refused at create time, for the
same reason.

**A completed reconciliation is evidence, so it holds the ledger down.**
Deleting an `AccountTransfer` hard-deletes its ledger rows (it has no void-memo
path), which would make a cleared row vanish from a closed period and silently
un-match a statement line. `AccountTransferDetailView.delete` therefore refuses
while any of its rows sit in a completed reconciliation — reopen it first. Any
new code path that *deletes* rather than reverses ledger rows needs the same
guard.

### Statement import

Two-phase like every other import here. `statement_import.parse` finds the
header row by name (statements carry preamble rows), accepts separate
debit/credit columns, a signed amount column, or an amount plus a direction
column, and handles both `1.234.567,89` and `1,234,567.89`. **An unsigned amount
column with no direction is rejected, not guessed** — guessing reverses half a
statement. `'K'` is likewise rejected: kredit in some exports, keluar in others.

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/accounting/reconciliations/` | List / start a period |
| GET/PATCH/DELETE | `/api/accounting/reconciliations/<pk>/` | Only the statement figures and notes are editable |
| GET | `.../<pk>/workspace/` | Lines + book entries + summary, in one round trip |
| POST | `.../<pk>/import/preview/` \| `.../import/confirm/` | Two-phase import (confirm also auto-matches) |
| POST | `.../<pk>/auto-match/` | Re-run the matcher |
| POST | `.../<pk>/lines/<line_pk>/<match\|unmatch\|ignore>/` | Returns the line **and** the recomputed summary |
| POST | `.../<pk>/complete/` \| `.../reopen/` | Close / undo |

Covered by `tests/test_bank_reconciliation.py`.

---

## Multi-Branch

One database, one chart of accounts, one patient registry — and a `Branch` row
per physical clinic that every operational document is stamped with.

### The two questions, answered separately

`managementsys/services/branches.py` is the only module that decides branch
access, and it answers two different questions with two different functions:

| Function | Answers | Used by |
|---|---|---|
| `write_branch(request, locked=)` | Which branch a **new document** is stamped with | Every create path |
| `read_branch_ids(request, locked=)` | Which branches a **query** may span (`None` = all) | Every list/report |
| `filter_by_branch(qs, request, ...)` | Applies the above to a queryset | List views |

`locked=True` is for POS, the patient queue, treatment sessions, beautician
petty cash and medical records. Those callers **ignore the client's branch
entirely** and use the user's `home_branch`. A cashier cannot book a sale into
another clinic's books by editing a request header, and neither can a manager.

`locked=False` is accounting and admin: the client's selection wins, subject to
`CROSS_BRANCH_ROLES` (`superuser`, `manager`). Everyone else silently collapses
to their home branch — a stale browser tab must never be able to 400 the app.

### The client states, the server decides

The browser sends `X-Branch-Id: <id>|all` on every request (`lib/apiFetch.ts`,
fed by `lib/branchStorage.ts`). `?branch=` is accepted as a fallback so a link
can carry it. **It is a preference, not a grant** — the role check happens
server-side on every request.

### `null` branch is a value, not a gap

`filter_by_branch` and `scope_to_branches` keep null-branch rows when a specific
branch is selected. That is deliberate: genuinely group-wide overhead and every
row posted before migration 0113 carry no branch, and excluding them would
produce branch P&Ls that quietly fail to reconcile with the group P&L. Pass
`include_null=False` only for operational lists (the queue, POS) where null is
legacy noise rather than shared cost. `read_branch_ids` returning `None` ("all
branches") is **not** the same as listing every branch id, for the same reason.

### Branch on the ledger is derived, never passed

`journal_engine.write_legs` denormalises `branch` onto both `JournalEntry` and
every `LedgerEntry`, taken from `document_branch_id(document, reverses,
corrects)` — the source document, never the request. A journal run sweeps
yesterday's invoices from a session that may have any branch selected; a
request-derived branch would stamp every one of them wrong. Reversals and
corrections inherit from the entry they act on, so a correction can never land
in a different branch's books than the mistake it fixes.

The one exception is a **manual journal**, which has no document — there the
operator's selection is passed as `branch_id=`, and a document always overrides
it.

### Models carrying `branch`

`ActivePatient`, `MedRec`, `TreatmentSession`, `Warehouse`, `Invoice`,
`PurchaseInvoice`, `Expense`, `AccountTransfer`, `StockOutLog`, `LedgerEntry`,
`JournalEntry`, `Appointment`, `AppointmentLocation`. Plus `AppUser.home_branch`.
All `PROTECT` and nullable; migration 0113 seeds `PUSAT` from `SiteConfig` and
backfills every pre-existing row to it.

### API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/branches/` | Any authenticated user. Returns only what *they* may select, plus the resolved current selection and `allow_all` |
| GET/POST | `/api/admin/branches/` | superuser/manager CRUD |
| GET/PUT/PATCH/DELETE | `/api/admin/branches/<id>/` | Delete is refused for the default branch and for any branch with history — deactivate instead |

Covered by `tests/test_branches.py`.

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
python manage.py poll_reservations        # Collect online bookings once
python manage.py poll_reservations --loop --interval 60   # what start-servers.bat runs
```

## Migrations

45 migrations. Two merge migrations (0039, 0040) resolved branching conflicts. Notable seed data in `0013_chartofaccounts_seed.py`. Always run `migrate` after pulling.
