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

    Returns a dict with the cash payment account and the three system fallbacks
    (4200000 revenue, 5100000 COGS, 1300000 inventory asset) referenced by
    ``_post_accounting`` when an item has no item_category.
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
    return {
        "cash": cash,
        "revenue": revenue,
        "cogs": cogs,
        "inventory_asset": inventory_asset,
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
