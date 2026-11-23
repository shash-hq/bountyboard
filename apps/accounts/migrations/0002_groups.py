from django.db import migrations

MODERATORS = "Moderators"
VERIFIERS = "Verifiers"


def _perm(apps, app_label, model, codename, name):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label=app_label, model=model)
    perm, _ = Permission.objects.get_or_create(
        codename=codename, content_type=ct, defaults={"name": name},
    )
    return perm


def create_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")

    moderate = _perm(apps, "bounties", "bounty", "moderate", "Can moderate bounties and reports")
    Group.objects.get_or_create(name=MODERATORS)[0].permissions.add(moderate)

    # Least privilege: verifiers hold ONLY this permission, so they cannot moderate listings or
    # see anyone's contact details.
    verify = _perm(apps, "accounts", "user", "verify_student", "Can verify students at a verification desk")
    Group.objects.get_or_create(name=VERIFIERS)[0].permissions.set([verify])


def remove_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=[MODERATORS, VERIFIERS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("bounties", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
