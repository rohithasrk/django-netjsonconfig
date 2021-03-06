# -*- coding: utf-8 -*-
# Generated by Django 1.10 on 2016-09-14 10:42
from __future__ import unicode_literals

from django.db import migrations


def randomize_mac_address(apps, schema_editor):
    """
    This data migration is not necessary anymore.
    It will be squashed in future versions.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('django_netjsonconfig', '0013_config_mac_address'),
    ]

    operations = [
        migrations.RunPython(randomize_mac_address, reverse_code=migrations.RunPython.noop),
    ]
