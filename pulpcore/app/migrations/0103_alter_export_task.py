# Generated by Django 3.2.18 on 2023-04-04 16:30

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0102_add_domain_relations'),
    ]

    operations = [
        migrations.AlterField(
            model_name='export',
            name='task',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.task'),
        ),
    ]