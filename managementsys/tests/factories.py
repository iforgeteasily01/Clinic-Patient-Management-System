"""factory_boy builders for the test suite.

Keep factories minimal — only fields required for a row to save. Tests override
whatever they assert on. Money values use whole rupiah; FIFO batch unit cost is
``value / quantity_initial``.
"""
import factory
from django.utils import timezone

from managementsys.models import (
    AppUser,
    ChartOfAccounts,
    InventoryBatch,
    InventoryItem,
    PaymentMethod,
    Warehouse,
)


class AppUserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AppUser
        skip_postgeneration_save = True  # the pin hook saves explicitly

    display_name = factory.Sequence(lambda n: f"Cashier {n}")
    role = "cashier"
    is_active = True

    @factory.post_generation
    def pin(obj, create, extracted, **kwargs):
        """Set a hashed PIN (default 123456). Usage: AppUserFactory(pin='654321')."""
        obj.set_pin(extracted or "123456")
        if create:
            obj.save()


class WarehouseFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Warehouse

    code = factory.Sequence(lambda n: f"WH{n}")
    name = "Main Store"
    is_active = True


class InventoryItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = InventoryItem

    code = factory.Sequence(lambda n: f"ITM{n:04d}")
    name = factory.Sequence(lambda n: f"Item {n}")
    selling_price = 10000
    unit_small = "pcs"
    is_active = True
    is_service = False


class InventoryBatchFactory(factory.django.DjangoModelFactory):
    """A FIFO batch. Default: 100 units worth 500000 -> 5000/unit COGS."""

    class Meta:
        model = InventoryBatch

    item = factory.SubFactory(InventoryItemFactory)
    warehouse = factory.SubFactory(WarehouseFactory)
    input_date = factory.LazyFunction(lambda: timezone.now().date())
    quantity_initial = 100
    quantity_remaining = 100
    value = 500000


class ChartOfAccountsFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ChartOfAccounts

    account_number = factory.Sequence(lambda n: 9000000 + n)
    name = factory.Sequence(lambda n: f"Account {n}")
    account_type = "asset"
    balance = 0


class PaymentMethodFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PaymentMethod

    name = factory.Sequence(lambda n: f"Payment Method {n}")
    linked_account = factory.SubFactory(ChartOfAccountsFactory)
    is_active = True
