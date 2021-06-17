"""Tasks for managing Debug Information Files from Apple App Store Connect.

Users can instruct Sentry to download dSYM from App Store Connect and put them into Sentry's
debug files.  These tasks enable this functionality.
"""

import logging
import tempfile

from sentry.api.endpoints.project_app_store_connect_credentials import get_app_store_config
from sentry.lang.native import appconnect
from sentry.models import AppConnectBuild, Project, debugfile
from sentry.tasks.base import instrumented_task
from sentry.utils.sdk import configure_scope

logger = logging.getLogger(__name__)


@instrumented_task(name="sentry.tasks.app_store_connect.dsym_download", queue="appstoreconnect")
def dsym_download(project_id: int, credentials_id: str):
    with configure_scope() as scope:
        scope.set_tag("project", project_id)

    project = Project.objects.get(pk=project_id)
    config = get_app_store_config(project, credentials_id)
    if config is None:
        raise KeyError("appStoreConnect symbol source config not found in project's symbol sources")
    client = appconnect.AppConnectClient.from_config(config)
    itunes_client = client.itunes_client()

    builds = client.list_builds()
    count = 0
    for build in builds:
        try:
            build_state = AppConnectBuild.objects.get(
                project=project,
                app_id=build.app_id,
                platform=build.platform,
                bundle_short_version=build.version,
                bundle_version=build.build_number,
            )
        except AppConnectBuild.DoesNotExist:
            build_state = AppConnectBuild(
                project=project,
                app_id=build.app_id,
                bundle_id=config["bundleId"],
                platform=build.platform,
                bundle_short_version=build.version,
                bundle_version=build.build_number,
                fetched=False,
            )

        if not build_state.fetched:
            with tempfile.NamedTemporaryFile() as dsyms_zip:
                itunes_client.download_dsyms(build, dsyms_zip.name)
                create_difs_from_dsyms_zip(dsyms_zip.name, project)
            build_state.fetched = True
            build_state.save()
            logger.debug("Uploaded dSYMs for build %s", build)

        count += 1
        if count >= 3:
            break


def create_difs_from_dsyms_zip(dsyms_zip: str, project: Project) -> None:
    with open(dsyms_zip, "rb") as fp:
        # TODO(flub): this kind of does some excessive logging for unknown file types in the
        #    zips, might turn into noisy sentry events.
        created = debugfile.create_files_from_dif_zip(fp, project)
        for proj_debug_file in created:
            logger.debug("Created %r for project %s", proj_debug_file, project.id)
