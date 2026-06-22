from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("slack_app", "0007_slackuserprofilecache_is_bot"),
    ]

    operations = [
        migrations.AddField(
            model_name="slacksettings",
            name="ai_runtime_adapter",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="slacksettings",
            name="ai_model",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="slacksettings",
            name="ai_reasoning_effort",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
    ]
