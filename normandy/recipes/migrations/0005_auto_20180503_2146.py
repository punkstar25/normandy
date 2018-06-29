# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2018-05-03 21:46
from __future__ import unicode_literals

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [("recipes", "0004_auto_20180502_2340")]

    operations = [
        migrations.RemoveField(model_name="approvalrequest", name="revision"),
        migrations.RemoveField(model_name="recipe", name="approved_revision"),
        migrations.RemoveField(model_name="recipe", name="latest_revision"),
        migrations.DeleteModel(name="RecipeRevision"),
        migrations.RenameModel("TmpRecipeRevision", "RecipeRevision"),
        migrations.AlterField(
            model_name="reciperevision",
            name="action",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="recipe_revisions",
                to="recipes.Action",
            ),
        ),
        migrations.AlterField(
            model_name="reciperevision",
            name="recipe",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="revisions",
                to="recipes.Recipe",
            ),
        ),
        migrations.AlterField(
            model_name="reciperevision",
            name="user",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="recipe_revisions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RenameField(
            model_name="approvalrequest", old_name="tmp_revision", new_name="revision"
        ),
        migrations.RenameField(
            model_name="recipe", old_name="approved_tmp_revision", new_name="approved_revision"
        ),
        migrations.RenameField(
            model_name="recipe", old_name="latest_tmp_revision", new_name="latest_revision"
        ),
        migrations.AlterField(
            model_name="approvalrequest",
            name="revision",
            field=models.OneToOneField(
                default=None,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="approval_request",
                to="recipes.RecipeRevision",
            ),
            preserve_default=False,
        ),
    ]
