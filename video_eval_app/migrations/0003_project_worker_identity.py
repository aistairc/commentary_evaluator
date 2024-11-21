# Generated by Django 5.1.3 on 2024-11-18 03:55

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('video_eval_app', '0002_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='worker_identity',
            field=models.IntegerField(choices=[(0, 'Anonymous'), (1, 'Hashed'), (2, 'Numbered'), (3, 'Username')], default=0),
        ),
        migrations.AlterField(
            model_name='datasetvideo',
            name='dataset',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='dataset_videos', to='video_eval_app.dataset'),
        ),
    ]
