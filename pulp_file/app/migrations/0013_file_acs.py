# Generated by Django 4.2.15 on 2024-10-22 12:40

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('file', '0012_delete_filefilesystemexporter'),
    ]

    operations = [
        migrations.CreateModel(
            name='FileAlternateContentSource',
            fields=[
                ('alternatecontentsource_ptr', models.OneToOneField(auto_created=True, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, related_name='file_filealternatecontentsource', serialize=False, to='core.alternatecontentsource')),
            ],
            options={
                'default_related_name': '%(app_label)s_%(model_name)s',
            },
            bases=('core.alternatecontentsource',),
        ),
    ]
