# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-04-08 15:44
from __future__ import unicode_literals

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_remove_locationtag_location'),
    ]

    operations = [
        migrations.AddField(
            model_name='locationtag',
            name='location',
            field=models.ForeignKey(
                default=0, on_delete=django.db.models.deletion.CASCADE, to='core.Location'
            ),
            preserve_default=False,
        ),
    ]
