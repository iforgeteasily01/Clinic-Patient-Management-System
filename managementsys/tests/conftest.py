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

settings.DATABASES["external"]["TEST"] = {"MIRROR": "default"}


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def auth_user(db):
    user = AppUserFactory(pin="654321")
    user.generate_token()
    return user


@pytest.fixture
def auth_api(api, auth_user):
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {auth_user.auth_token}")
    return api


@pytest.fixture
def gl_accounts(db):
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
    bank = ChartOfAccountsFactory(
        account_number=1102000, name="Bank BCA",
        account_type="asset", is_head=False, parent=cash_head,
    )
    legacy_clearing = ChartOfAccountsFactory(
        account_number=1100011, name="Kas Penjualan iPos (histori)",
        account_type="asset", is_head=False, parent=cash_head,
    )
    opening_equity = ChartOfAccountsFactory(
        account_number=3900000, name="Opening Balance Equity", account_type="equity",
    )
    cash_method = PaymentMethodFactory(name="Cash", linked_account=cash)
    legacy_clearing_method = PaymentMethodFactory(
        name="Kas Penjualan iPos (histori)", linked_account=legacy_clearing,
        is_active=False,
    )
    return {
        "cash": cash,
        "revenue": revenue,
        "cogs": cogs,
        "inventory_asset": inventory_asset,
        "sales_discount": sales_discount,
        "tax_payable": tax_payable,
        "additional_charges": additional_charges,
        "bank": bank,
        "legacy_clearing": legacy_clearing,
        "opening_equity": opening_equity,
        "cash_method": cash_method,
        "legacy_clearing_method": legacy_clearing_method,
    }


@pytest.fixture
def stock(db):
    warehouse = WarehouseFactory()
    item = InventoryItemFactory(selling_price=10000)
    batch = InventoryBatchFactory(
        item=item, warehouse=warehouse,
        quantity_initial=Decimal("100"), quantity_remaining=Decimal("100"),
        value=Decimal("500000"),
    )
    return {"warehouse": warehouse, "item": item, "batch": batch}
