# iPos Database Reference

**Version:** IPOS-4.0 (update 93)  
**Engine:** PostgreSQL  
**Connection (dev):** `dbname=ipos user=postgres password=seesaw host=localhost port=5432`  
**Django alias:** `external` (configured in `CPMS/settings.py` via `EXTERNAL_DB_*` env vars)

---

## Key Warnings

- **`tbl_item.stok` is always 0.** It is not updated by transactions. Do not use it for stock queries.
- **Canonical per-branch stock is in `tbl_itemstok`.** One row per `(kodeitem, kantor)` pair.
- **`tbl_item.hargapokok` (buying price) can be 0** even for active items. The authoritative buying price per unit/branch is in `tbl_itemsatuanjml.hargapokok`.
- All reads are safe. Never run UPDATE/INSERT/DELETE on this database — it is a live legacy POS system.
- Django's `using('external')` router must be used for all queries against this DB from within the Django app.

---

## Branches (`tbl_kantor`)

| `kodekantor` | `fungsi` | `namakantor` |
|---|---|---|
| `GD` | Gudang | Gudang (Warehouse) |
| `UTM` | Utama | Utama (Main/Retail) |

All per-branch tables (stock, sales, purchases, users) reference `kodekantor`.

---

## Core Item Tables

### `tbl_item` — Item Catalogue (688 rows)

Primary key: `kodeitem` (varchar 30)

| Column | Type | Notes |
|---|---|---|
| `kodeitem` | varchar(30) | PK — item code, used as the join key everywhere |
| `namaitem` | varchar | Display name |
| `jenis` | varchar | Category code → FK `tbl_itemjenis.jenis` |
| `tipe` | varchar(1) | Item type: `1`=goods, `2`=service |
| `satuan` | varchar | Default unit → FK `tbl_itemsatuan.satuan` |
| `hargapokok` | numeric | Buying price (may be 0 — see warning above) |
| `hargajual1` | numeric | Default selling price |
| `prhargajual1` | numeric | Selling price percentage (if price system is %) |
| `sistemhargajual` | varchar | `'O'` = fixed price, `'P'` = percentage |
| `statusjual` | varchar(1) | `'Y'` = active, `'N'` = inactive |
| `stokmin` | numeric | Minimum stock level |
| `dept` | varchar | Home branch → FK `tbl_kantor.kodekantor` |
| `supplier1` | varchar | Default supplier code → FK `tbl_supel.kode` |
| `merek` | varchar | Brand → FK `tbl_itemmerek.merek` |
| `keterangan` | varchar | Notes / description |
| `stok` | numeric | **Always 0 — do not use** |
| `statushapus` | varchar | Soft-delete flag (`null` = active) |
| `acc_hpp` | varchar | GL account: COGS |
| `acc_pendapatan` | varchar | GL account: revenue |
| `acc_persediaan` | varchar | GL account: inventory asset |
| `acc_jasa` | varchar | GL account: service revenue |
| `dateupd` | timestamp | Last modified |

**Useful filter:** `statusjual = 'Y'` for active items only. Items with `tipe = '2'` are services (no physical stock).

---

### `tbl_itemstok` — Per-Branch Stock (382 rows)

The **only** reliable source of current stock quantities.

| Column | Type | Notes |
|---|---|---|
| `kodeitem` | varchar | FK → `tbl_item.kodeitem` |
| `kantor` | varchar | Branch code → FK `tbl_kantor.kodekantor` |
| `stok` | numeric | Current quantity on hand |

PK is `(kodeitem, kantor)`. Not every item has a row here — absence means 0 stock.

**Common query — all items with stock by branch:**
```sql
SELECT i.kodeitem, i.namaitem, i.jenis, i.hargapokok,
       COALESCE(s_gd.stok, 0) AS stok_gd,
       COALESCE(s_utm.stok, 0) AS stok_utm
FROM tbl_item i
LEFT JOIN tbl_itemstok s_gd ON i.kodeitem = s_gd.kodeitem AND s_gd.kantor = 'GD'
LEFT JOIN tbl_itemstok s_utm ON i.kodeitem = s_utm.kodeitem AND s_utm.kantor = 'UTM'
WHERE i.statusjual = 'Y'
  AND i.tipe = '1'
ORDER BY i.kodeitem;
```

---

### `tbl_itemhj` — Selling Price Tiers (382 rows)

Stores tier/level-based selling prices per item. An item may have multiple rows (different price levels or quantity breaks).

| Column | Type | Notes |
|---|---|---|
| `iddetail` | varchar | PK (auto-generated composite key) |
| `kodeitem` | varchar | FK → `tbl_item.kodeitem` |
| `tipehj` | varchar | Price type (`'S'` = standard) |
| `level` | int | Price level (0 = default) |
| `satuan` | varchar | Unit |
| `hargajual` | numeric | Selling price |
| `jmlsampai` | numeric | Quantity up-to (0 = unlimited) |
| `prosentase` | numeric | Percentage (if sistem = percentage) |
| `dateupd` | timestamp | Last modified |

---

### `tbl_itemsatuanjml` — Item Units with Buying Price (688 rows)

One row per `(item, unit, branch)`. Contains the **per-unit buying price** which is more reliable than `tbl_item.hargapokok`.

| Column | Type | Notes |
|---|---|---|
| `iddetail` | varchar | PK |
| `kodeitem` | varchar | FK → `tbl_item.kodeitem` |
| `satuan` | varchar | Unit |
| `jumlahkonv` | numeric | Conversion factor (1.0 = base unit) |
| `kodebarcode` | varchar | Barcode (nullable) |
| `hargapokok` | numeric | Buying price for this unit |
| `tipe` | varchar | `'D'` = default |
| `dateupd` | timestamp | |

---

### `tbl_itemsatuan` — Unit Definitions (11 rows)

| Column | Type | Notes |
|---|---|---|
| `satuan` | varchar | PK — unit code (e.g. `PCS`, `BOX`, `DUS`, `kali`) |
| `ketsatuan` | varchar | Description |
| `konversi` | numeric | Conversion rate (0 = base) |
| `satuankonversi` | varchar | Target unit for conversion |
| `utama` | boolean | Is base unit |

---

### `tbl_itemjenis` — Item Categories (15 rows)

| `jenis` | `ketjenis` |
|---|---|
| `OBAT MINUM` | OBAT MINUM |
| `OBAT SUNTIK` | OBAT SUNTIK |
| `SERUM` | SERUM |
| `CREAM` | CREAM |
| `TONER` | TONER |
| `BEDAK` | BEDAK |
| `TUBE` | TUBE |
| `SABUN` | SABUN |
| `SHAMPOO` | SHAMPOO |
| `TONIC` | TONIC |
| `Jasa` | Jasa (services) |
| `KALI` | KALI |
| `LAIN-LAIN` | LAIN-LAIN |
| `MKN` | Makanan |
| `MNM` | Minuman |

---

### `tbl_itemmerek` — Brands (5 rows)

| `merek` | `ketmerek` |
|---|---|
| (blank) | (blank) |
| `tome` | MEDYA |
| `medya` | — |
| `Medya` | Medya |
| `APOLLO` | APOLLO |

---

## Transaction Tables

### Sales

**`tbl_ikhd`** — Sales Header (24,859 rows)

| Column | Notes |
|---|---|
| `notransaksi` | PK — transaction number (e.g. `0099/KSR/GD/0324`) |
| `kodekantor` | Branch where sale occurred |
| `tanggal` | Transaction datetime |
| `tipe` | `IK`=invoice, `KSR`=cashier/retail, `RJ`=sales return |
| `kodesupel` | Customer code → FK `tbl_supel.kode` |
| `totalakhir` | Final total |
| `carabayar` | Payment method |
| `user1` | Cashier username |

**`tbl_ikdt`** — Sales Detail (68,372 rows)

| Column | Notes |
|---|---|
| `iddetail` | PK |
| `notransaksi` | FK → `tbl_ikhd.notransaksi` |
| `kodeitem` | FK → `tbl_item.kodeitem` |
| `jumlah` | Quantity sold |
| `satuan` | Unit |
| `harga` | Unit price at time of sale |
| `total` | Line total |

---

### Purchases

**`tbl_imhd`** — Purchase Header (1,661 rows)

| Column | Notes |
|---|---|
| `notransaksi` | PK |
| `kodekantor` | Receiving branch |
| `tanggal` | Date |
| `tipe` | `BL`=purchase, `IM`=inter-branch transfer, `SA`=stock adjustment/opening balance |
| `kodesupel` | Supplier code → FK `tbl_supel.kode` |
| `totalakhir` | Total amount |

**`tbl_imdt`** — Purchase Detail (4,054 rows)

| Column | Notes |
|---|---|
| `iddetail` | PK |
| `notransaksi` | FK → `tbl_imhd.notransaksi` |
| `kodeitem` | FK → `tbl_item.kodeitem` |
| `jumlah` | Quantity received |
| `satuan` | Unit |
| `harga` | Unit buying price at time of purchase |
| `total` | Line total |
| `tglexp` | Expiry date (nullable) |

---

## Reference / Lookup Tables

### `tbl_supel` — Suppliers & Customers (16,443 rows)

Unified table for both suppliers and customers.

| Column | Notes |
|---|---|
| `kode` | PK |
| `tipe` | `S`=supplier, `C`=customer, `D`=both |
| `nama` | Name |
| `alamat` | Address |
| `telepon` | Phone |
| `email` | Email |
| `matauang` | Currency |

---

### `tbl_perkiraan` — Chart of Accounts (91 rows)

| Column | Notes |
|---|---|
| `kodeacc` | PK (e.g. `1-1110`, `5-1100`) |
| `parentacc` | Parent account code |
| `kelompok` | Group number (1=asset, 2=liability, 3=equity, 4=revenue, 5=COGS, 6=expense, 7=other income) |
| `tipe` | `H`=header, `D`=detail |
| `namaacc` | Account name |

Referenced extensively in transaction headers for GL posting.

---

### `tbl_user` — POS Users (6 rows)

| Column | Notes |
|---|---|
| `userid` | PK / login name |
| `nama` | Display name |
| `password` | Plaintext (legacy) |
| `tipe` | `AU`=regular |
| `loginkantor` | Home branch |
| `kelompok` | Role group → FK `tbl_userg.kelompok` |

Known users: IWAN (UTM/SUPERVISOR), KASIR (UTM/KASIR), ATIKAH (GD/ADMIN), ADMIN (GD/ADMIN), MEDYA (GD/ADMIN), AINUN (GD/KASIR).

---

### `tbl_conf` — System Configuration (56 rows)

| Column | Notes |
|---|---|
| `confname` | Setting name |
| `confvalue` | Value |

Notable settings:
- `METHOD_HPP = 'FIFO'` — inventory costing method is FIFO

---

## Inventory Method

Stock is tracked using **FIFO** (`tbl_conf.METHOD_HPP = 'FIFO'`). iPos does not expose individual FIFO batches in a separate table — the current cost in `tbl_itemsatuanjml.hargapokok` reflects the running weighted/FIFO average at last update.

---

## Rarely Used / Empty Tables

These tables exist in the schema but have 0 or very few rows in this installation:

| Table | Purpose | Rows |
|---|---|---|
| `tbl_itemopname` | Stock opname/count adjustments | 0 |
| `tbl_itemrakitan` | Bill of materials / assembly | 0 |
| `tbl_itemserial` | Serial number tracking | 0 |
| `tbl_pesanhd/dt` | Purchase orders | 0 |
| `tbl_itrdt/hd` | Inter-branch transfers | 0 |
| `tbl_itempotongan` | Item-level discounts | 0 |
| `tbl_itemdisp` | Display/promotion items | 0 |
| `tbl_accjurnal` | Manual journal entries | 0 |

---

## Useful Queries

**All physical goods with category and per-branch stock:**
```sql
SELECT
    i.kodeitem, i.namaitem, i.jenis, i.satuan,
    i.hargapokok AS buying_price,
    COALESCE(s_gd.stok, 0) AS stok_gd,
    COALESCE(s_utm.stok, 0) AS stok_utm
FROM tbl_item i
LEFT JOIN tbl_itemstok s_gd  ON i.kodeitem = s_gd.kodeitem  AND s_gd.kantor  = 'GD'
LEFT JOIN tbl_itemstok s_utm ON i.kodeitem = s_utm.kodeitem AND s_utm.kantor = 'UTM'
WHERE i.tipe = '1' AND i.statusjual = 'Y'
ORDER BY i.kodeitem;
```

**Items with stock > 0 in GD (warehouse):**
```sql
SELECT i.kodeitem, i.namaitem, s.stok
FROM tbl_item i
JOIN tbl_itemstok s ON i.kodeitem = s.kodeitem
WHERE s.kantor = 'GD' AND s.stok > 0
ORDER BY i.namaitem;
```

**Recent purchases for an item:**
```sql
SELECT h.tanggal, h.notransaksi, d.jumlah, d.harga, d.total
FROM tbl_imdt d
JOIN tbl_imhd h ON d.notransaksi = h.notransaksi
WHERE d.kodeitem = 'YOUR_KODEITEM'
ORDER BY h.tanggal DESC
LIMIT 20;
```

**Buying price per unit from `tbl_itemsatuanjml` (more reliable than `tbl_item.hargapokok`):**
```sql
SELECT kodeitem, satuan, hargapokok
FROM tbl_itemsatuanjml
WHERE kodeitem = 'YOUR_KODEITEM';
```

---

## Name Matching Notes

iPos item names and codes frequently diverge from external files (stock sheets, other systems). When doing fuzzy matching:

1. Normalize: `re.sub(r"\s+", "", str(s).lower().strip())` — removes all whitespace, lowercases
2. Match priority: exact code → exact name → prefix partial on code → prefix partial on name
3. Common discrepancies:
   - Commas vs dots in numbers: `R 1,5` → `R1.5`
   - Abbreviated vs full names: `Kito Milky Toner` → `Kitoderm pro+ milky toner`
   - Typos: `Cosmels Acne Ceramoist` → `COSMELS Acne Ceramost`
   - Word order swaps: `Cosmels Serum Glow` → `Cosmels Glow Serum`

The OVERRIDES dict in `scripts/prepare_import.py` contains 50+ known mappings from stock-file names to iPos `kodeitem`.
