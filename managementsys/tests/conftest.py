"""Shared pytest fixtures.

Covers:
  * an unauthenticated and an authenticated DRF APIClient
  * the chart-of-accounts skeleton the POS endpoint needs (cash payment method +
    the system fallback revenue/COGS/inventory accounts)
  * a ready-to-sell stock setup

The module also points the secondary ``external`` database at the same test DB
as ``default`` so the test runner does not try to create/connect to a separate
``test_ipos`` database (the app never writes to it in these tests).
"""
from decimal import Decimal

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from .factories import (
    AppUserFactory,
    ChartOfAccountsFactory,
    InventoryBatchFactory,
    InventoryItemFactory,
    PaymentMethodFactory,
    WarehouseFactory,
)

# Mirror the external DB onto default so no second test database is provisioned.
settings.DATABASES["external"]["TEST"] = {"MIRROR": "default"}


# ── Clients ────────────────────────────────────────────────────────────────────

@pytest.fixture
def api():
    """Anonymous DRF client."""
    return APIClient()


@pytest.fixture
def auth_user(db):
    """A logged-in AppUser carrying a valid bearer token."""
    user = AppUserFactory(pin="654321")
    user.generate_token()
    return user


@pytest.fixture
def auth_api(api, auth_user):
    """DRF client pre-loaded with auth_user's bearer token."""
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {auth_user.auth_token}")
    return api


# ── Accounting / inventory scaffolding ──────────────────────────────────────────

@pytest.fixture
def gl_accounts(db):
    """Create the GL accounts the invoice posting logic relies on.

    Returns a dict with the cash payment account and the system fallbacks
    (4200000 revenue, 5100000 COGS, 1300000 inventory asset) referenced by
    ``_post_accounting`` when an item has no item_category, plus the accounts the
    non-line parts of an invoice post to (4100000 discount, 2200000 tax,
    7100000 additional charges) — without those an invoice carrying a discount,
    tax or a surcharge cannot balance.
    """
    cash_head = ChartOfAccountsFactory(
        account_number=1100000, name="Cash & Equivalents",
        account_type="asset", is_head=True,
    )
    cash = ChartOfAccountsFactory(
        account_number=1101000, name="Cash Drawer",
        account_type="asset", is_head=False, parent=cash_head,
    )
    revenue = ChartOfAccountsFactory(
        account_number=4200000, name="Sales Revenue", account_type="revenue",
    )
    cogs = ChartOfAccountsFactory(
        account_number=5100000, name="Cost of Goods Sold", account_type="cogs",
    )
    inventory_asset = ChartOfAccountsFactory(
        account_number=1300000, name="Inventory", account_type="asset",
    )
    sales_discount = ChartOfAccountsFactory(
        account_number=4100000, name="Sales Discount", account_type="revenue",
    )
    tax_payable = ChartOfAccountsFactory(
        account_number=2200000, name="Tax Payable", account_type="liability",
    )
    additional_charges = ChartOfAccountsFactory(
        account_number=7100000, name="Additional Charges", account_type="other_income",
    )
    undeposited = ChartOfAccountsFactory(
        account_number=1100011, name="Undeposited Funds",
        account_type="asset", is_head=False, parent=cash_head,
    )
    opening_equity = ChartOfAccountsFactory(
        account_number=3900000, name="Opening Balance Equity", account_type="equity",
    )
    # PaymentMethod rows — what invoice/purchase-invoice creation actually points
    # at now that "how the customer paid" is decoupled from the GL account.
    cash_method = PaymentMethodFactory(name="Cash", linked_account=cash)
    undeposited_method = PaymentMethodFactory(name="Undeposited Funds", linked_account=undeposited)
    return {
        "cash": cash,
        "revenue": revenue,
        "cogs": cogs,
        "inventory_asset": inventory_asset,
        "sales_discount": sales_discount,
        "tax_payable": tax_payable,
        "additional_charges": additional_charges,
        "undeposited": undeposited,
        "opening_equity": opening_equity,
        "cash_method": cash_method,
        "undeposited_method": undeposited_method,
    }


@pytest.fixture
def stock(db):
    """A warehouse holding 100 units of one item at 5000/unit COGS."""
    warehouse = WarehouseFactory()
    item = InventoryItemFactory(selling_price=10000)
    batch = InventoryBatchFactory(
        item=item, warehouse=warehouse,
        quantity_initial=Decimal("100"), quantity_remaining=Decimal("100"),
        value=Decimal("500000"),
    )
    return {"warehouse": warehouse, "item": item, "batch": batch}
