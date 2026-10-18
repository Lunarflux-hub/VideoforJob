from decimal import Decimal

from django.db import migrations, models


def set_promo_on_starter(apps, schema_editor):
    GenerationPackage = apps.get_model("billing", "GenerationPackage")
    starter, _ = GenerationPackage.objects.update_or_create(
        title="Стартовый",
        defaults=dict(
            price=Decimal("99.00"),
            generations=1,
            is_active=True,
            first_purchase_price=Decimal("49.00"),
        ),
    )


def unset_promo(apps, schema_editor):
    GenerationPackage = apps.get_model("billing", "GenerationPackage")
    GenerationPackage.objects.filter(title="Стартовый").update(first_purchase_price=None)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0005_backfill_userbalance"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationpackage",
            name="first_purchase_price",
            field=models.DecimalField(
                max_digits=10,
                decimal_places=2,
                null=True,
                blank=True,
                help_text="Спеццена для первой покупки пользователя (акция). Пусто — без акции.",
            ),
        ),
        migrations.RunPython(set_promo_on_starter, unset_promo),
    ]
