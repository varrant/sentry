"""Integration of native symbolication with Apple App Store Connect.

Sentry can download dSYMs directly from App Store Connect, this is the support code for
this.
"""

import enum
import io
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

import jsonschema
import requests

from sentry.lang.native.symbolicator import APP_STORE_CONNECT_SCHEMA
from sentry.models import Project
from sentry.utils import json
from sentry.utils.appleconnect import appstore_connect, itunes_connect

logger = logging.getLogger(__name__)


# The key in the project options under which all symbol sources are stored.
SYMBOL_SOURCES_PROP_NAME = "sentry:symbol_sources"


class InvalidCredentialsError(Exception):
    """Invalid credentials for the App Store Connect API."""

    pass


class InvalidConfigError(Exception):
    """Invalid configuration for the appStoreConnect symbol source."""

    pass


class NoDsymsError(Exception):
    """No dSYMs were found."""

    pass


# @dataclass(frozen=True)
# class AppInfo:
#     """Information about an application on App Store Connect."""

#     # The bundle ID, e.g. ``io.sentry.sample.iOS-Swift``.
#     bundle_id: str

#     # The API ID of this app.
#     id: int

#     # The human-readable name of the app.
#     name: str


@enum.unique
class BuildKind(enum.Enum):
    ALL = 1
    PRE_RELEASE = 2
    RELEASE = 3


@dataclass(frozen=True)
class BuildInfo:
    """Information about an App Store Connect build.

    A build is identified by the tuple of (platform, short_version, bundle_id, version).
    """

    # The kind of build, either PRE_RELEASE or RELEASE
    kind: BuildKind

    # The app ID
    app_id: str

    # A platform identifying e.g. iOS, TvOS etc.
    #
    # These are not human readable but some opaque string supplied by apple.
    platform: str

    # The human-readable version, e.g. "7.2.0".
    #
    # Each version can have multiple builds, Apple naming is a little confusing and calls
    # this "bundle_short_version".
    version: str

    # The build number, typically just a monotonically increasing number.
    #
    # Apple naming calls this the "bundle_version".
    build_number: str


class ITunesClient:
    """A client for the legacy iTunes API.

    Create this by calling :class:`AppConnectClient.itunes_client()`.

    On creation this will contact iTunes and will fail if it does not have a valid iTunes
    session.
    """

    def __init__(self, itunes_cookie: str, itunes_org: int):
        self._session = requests.Session()
        itunes_connect.load_session_cookie(self._session, itunes_cookie)
        # itunes_connect.set_provider(self._session, itunes_org)

    def download_dsyms(self, build: BuildInfo, path: str) -> None:
        # TODO(flub): is there a better type for the path?
        url = itunes_connect.get_dsym_url(
            self._session, build.app_id, build.version, build.build_number, build.platform
        )
        if not url:
            raise NoDsymsError
        logger.debug("Fetching dSYM from: %s", url)
        with requests.get(url, stream=True) as req:
            req.raise_for_status()
            with open(path, "wb") as fp:
                for chunk in req.iter_content(chunk_size=io.DEFAULT_BUFFER_SIZE):
                    fp.write(chunk)


class AppConnectClient:
    """Client to interact with a single app from App Store Connect.

    Note that on creating this instance it will already connect to iTunes to set the
    provider for this session.  You also don't want to use the same iTunes cookie in
    multiple connections, so only make one client for a project.
    """

    def __init__(
        self,
        api_credentials: appstore_connect.AppConnectCredentials,
        itunes_cookie: str,
        itunes_org: int,
        app_id: str,
    ) -> None:
        """Internal init, use one of the classmethods instead."""
        self._api_credentials = api_credentials
        self._session = requests.Session()
        self._itunes_cookie = itunes_cookie
        self._itunes_org = itunes_org
        self._app_id = app_id

    @classmethod
    def from_project(cls, project: Project, credentials_id: str) -> "AppConnectClient":
        """Creates a new client for the project's appStoreConnect symbol source.

        This will load the configuration from the symbol sources for the project if a symbol
        source of the ``appStoreConnect`` type can be found which also has matching
        ``credentials_id``.
        """
        all_sources_raw = project.get_option(SYMBOL_SOURCES_PROP_NAME)
        all_sources = json.loads(all_sources_raw)

        for source in all_sources:
            if source.get("type") == "appStoreConnect" and source.get("id") == credentials_id:
                config = source
                break
        else:
            raise KeyError("Config not found in configured symbol sources")

        return cls.from_config(config)

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "AppConnectClient":
        """Creates a new client from an appStoreConnect symbol source config.

        This config is normally stored as a symbol source of type ``appStoreConnect`` i an
        project's ``sentry:symbol_sources`` property.
        """
        try:
            jsonschema.validate(config, APP_STORE_CONNECT_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise InvalidConfigError from e

        api_credentials = appstore_connect.AppConnectCredentials(
            key_id=config["appconnectKey"],
            key=config["appconnectPrivateKey"],
            issuer_id=config["appconnectIssuer"],
        )
        itunes_cookie = config["itunesSession"]
        itunes_org = int(config["orgId"])
        app_id = config["appId"]
        return cls(
            api_credentials=api_credentials,
            itunes_cookie=itunes_cookie,
            itunes_org=itunes_org,
            app_id=app_id,
        )

    def itunes_client(self) -> ITunesClient:
        """Returns an iTunes client capable of downloading dSYMs.

        This will raise an exception if the session cookie is expired.
        """
        return ITunesClient(itunes_cookie=self._itunes_cookie, itunes_org=self._itunes_org)

    # def list_apps(self) -> List[AppInfo]:
    #     """Returns the available apps.

    #     :raises InvalidCredentialsException: If the API credentials do not work.
    #     """
    #     # This also initialises the self._bundle_id_to_app_id cache used by self._app_id()
    #     apps = appstore_connect.get_apps(self._session, self._api_credentials)
    #     if apps is None:
    #         raise InvalidCredentialsException()

    #     all_apps = []
    #     for info in apps:
    #         app = AppInfo(bundle_id=info.bundle_id, id=info.app_id, name=info.name)
    #         all_apps.append(app)

    #     return all_apps

    def list_builds(self, kind: BuildKind = BuildKind.ALL) -> List[BuildInfo]:
        """Returns the available builds, grouped by release.

        :param kind: Whether to only query pre-releases or only releases or all.
        :param bundle: The bundle ID, e.g. ``io.sentry.sample.iOS-Swift``.
        """
        if kind == BuildKind.PRE_RELEASE:
            ret = appstore_connect.get_pre_release_version_info(
                self._session, self._api_credentials, self._app_id
            )
            all_results = {"pre_releases": ret}
        elif kind == BuildKind.RELEASE:
            ret = appstore_connect.get_pre_release_version_info(
                self._session, self._api_credentials, self._app_id
            )
            all_results = {"releases": ret}
        else:
            all_results = appstore_connect.get_build_info(
                self._session, self._api_credentials, self._app_id
            )

        builds = []
        for kind_name, results in all_results.items():
            if kind_name == "pre_releases":
                kind = BuildKind.PRE_RELEASE
            else:
                kind = BuildKind.RELEASE

            for release in results:
                for build in release["versions"]:
                    build = BuildInfo(
                        kind=kind,
                        app_id=self._app_id,
                        platform=release["platform"],
                        version=release["short_version"],
                        build_number=build["version"],
                    )
                    builds.append(build)
        return builds
