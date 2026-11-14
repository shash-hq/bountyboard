from django.db import migrations


def create_moderators_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    
    # Get or create content type for Bounty
    bounty_ct, _ = ContentType.objects.get_or_create(
        app_label="bounties",
        model="bounty",
    )
    
    # Get or create moderate permission
    moderate_perm, _ = Permission.objects.get_or_create(
        codename="moderate",
        content_type=bounty_ct,
        defaults={"name": "Can moderate bounties and reports"},
    )
    
    # Create Moderators group and assign permission
    group, _ = Group.objects.get_or_create(name="Moderators")
    group.permissions.add(moderate_perm)


def remove_moderators_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name="Moderators").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "__latest__"),
        ("contenttypes", "__latest__"),
        ("bounties", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_moderators_group, remove_moderators_group),
    ]
