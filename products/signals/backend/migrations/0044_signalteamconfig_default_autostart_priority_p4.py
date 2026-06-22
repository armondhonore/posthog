from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("signals", "0043_signalreporttask_rel_idx"),
    ]

    operations = [
        migrations.AlterField(
            model_name="signalteamconfig",
            name="default_autostart_priority",
            field=models.CharField(
                choices=[("P0", "P0"), ("P1", "P1"), ("P2", "P2"), ("P3", "P3"), ("P4", "P4")],
                default="P4",
                max_length=2,
            ),
        ),
    ]
