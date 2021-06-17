# Generated by Django 1.11.29 on 2021-05-28 01:09

from django.db import migrations, models


class Migration(migrations.Migration):
    # This flag is used to mark that a migration shouldn't be automatically run in
    # production. We set this to True for operations that we think are risky and want
    # someone from ops to run manually and monitor.
    # General advice is that if in doubt, mark your migration as `is_dangerous`.
    # Some things you should always mark as dangerous:
    # - Large data migrations. Typically we want these to be run manually by ops so that
    #   they can be monitored. Since data migrations will now hold a transaction open
    #   this is even more important.
    # - Adding columns to highly active tables, even ones that are NULL.
    is_dangerous = True

    # This flag is used to decide whether to run this migration in a transaction or not.
    # By default we prefer to run in a transaction, but for migrations where you want
    # to `CREATE INDEX CONCURRENTLY` this needs to be set to False. Typically you'll
    # want to create an index concurrently when adding one to an existing table.
    # You'll also usually want to set this to `False` if you're writing a data
    # migration, since we don't want the entire migration to run in one long-running
    # transaction.
    atomic = False

    dependencies = [
        ("sentry", "0200_release_indices"),
    ]

    operations = [
        migrations.AddField(
            model_name="release",
            name="package",
            field=models.TextField(null=True),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    """
                    DROP INDEX CONCURRENTLY IF EXISTS "sentry_release_organization_id_major_mi_38715957_idx";
                    """,
                    reverse_sql="""
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS "sentry_release_organization_id_major_mi_38715957_idx"
                    ON "sentry_release" ("organization_id", "major" DESC, "minor" DESC, "patch" DESC, "revision" DESC);
                    """,
                ),
                migrations.RunSQL(
                    """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS "sentry_release_semver_idx"
                    ON "sentry_release" (
                    "organization_id",
                    "major" DESC,
                    "minor" DESC,
                    "patch" DESC,
                    "revision" DESC,
                    (CASE
                        WHEN prerelease = ''::text THEN 1
                        ELSE 0
                    END) DESC,
                    prerelease DESC);
                    """,
                    reverse_sql="DROP INDEX CONCURRENTLY IF EXISTS sentry_release_semver_idx",
                ),
                migrations.RunSQL(
                    """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS "sentry_release_semver_by_package_idx"
                    ON "sentry_release" (
                    "organization_id",
                    "package",
                    "major" DESC,
                    "minor" DESC,
                    "patch" DESC,
                    "revision" DESC,
                    (CASE
                        WHEN prerelease = ''::text THEN 1
                        ELSE 0
                    END) DESC,
                    prerelease DESC);
                    """,
                    reverse_sql="DROP INDEX CONCURRENTLY IF EXISTS sentry_release_semver_by_package_idx",
                ),
            ],
            state_operations=[
                migrations.AlterIndexTogether(
                    name="release",
                    index_together={
                        ("organization", "date_added"),
                        ("organization", "build_code"),
                        ("organization", "build_number"),
                        ("organization", "status"),
                        (
                            "organization",
                            "package",
                            "major",
                            "minor",
                            "patch",
                            "revision",
                            "prerelease",
                        ),
                        ("organization", "major", "minor", "patch", "revision", "prerelease"),
                    },
                ),
            ],
        ),
    ]
