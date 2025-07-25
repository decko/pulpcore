"""
Repository related Django models.
"""

from contextlib import suppress
from gettext import gettext as _
from os import path
from collections import defaultdict
import logging

import django
from asyncio_throttle import Throttler
from django.conf import settings
from django.contrib.postgres.fields import HStoreField, ArrayField
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F, Func, Q, Value
from django_lifecycle import AFTER_UPDATE, BEFORE_CREATE, BEFORE_DELETE, hook
from rest_framework.exceptions import APIException

from pulpcore.app.util import (
    batch_qs,
    get_prn,
    get_view_name_for_model,
    get_domain_pk,
    cache_key,
    reverse,
)
from pulpcore.constants import ALL_KNOWN_CONTENT_CHECKSUMS, PROTECTED_REPO_VERSION_MESSAGE
from pulpcore.download.factory import DownloaderFactory
from pulpcore.exceptions import ResourceImmutableError

from pulpcore.cache import Cache

from .base import MasterModel, BaseModel
from .content import Artifact, Content, ContentArtifact, RemoteArtifact
from .fields import EncryptedTextField
from .task import CreatedResource, Task


_logger = logging.getLogger(__name__)


class Repository(MasterModel):
    """
    Collection of content.

    Fields:

        name (models.TextField): The repository name.
        pulp_labels (HStoreField): Dictionary of string values.
        description (models.TextField): An optional description.
        next_version (models.PositiveIntegerField): A record of the next version number to be
            created.
        retain_repo_versions (models.PositiveIntegerField): Number of repo versions to keep
        user_hidden (models.BooleanField): Whether to expose this repo to users via the API

    Relations:

        content (models.ManyToManyField): Associated content.
        remote (models.ForeignKeyField): Associated remote
        pulp_domain (models.ForeignKeyField): The domain the Repository is a part of.
    """

    TYPE = "repository"
    CONTENT_TYPES = []
    REMOTE_TYPES = []
    PULL_THROUGH_SUPPORTED = False

    name = models.TextField(db_index=True)
    pulp_labels = HStoreField(default=dict)
    description = models.TextField(null=True)
    next_version = models.PositiveIntegerField(default=0)
    retain_repo_versions = models.PositiveIntegerField(default=None, null=True)
    user_hidden = models.BooleanField(default=False)
    content = models.ManyToManyField(
        "Content", through="RepositoryContent", related_name="repositories"
    )
    remote = models.ForeignKey("Remote", null=True, on_delete=models.SET_NULL)
    pulp_domain = models.ForeignKey("Domain", default=get_domain_pk, on_delete=models.PROTECT)

    class Meta:
        unique_together = ("name", "pulp_domain")
        verbose_name_plural = "repositories"

    @property
    def disk_size(self):
        """Returns the approximate size on disk for all artifacts stored across all versions."""
        all_content = (
            RepositoryContent.objects.filter(repository=self)
            .distinct("content")
            .values_list("content")
        )
        return (
            Artifact.objects.filter(content__in=all_content)
            .distinct()
            .aggregate(size=models.Sum("size", default=0))["size"]
        )

    @property
    def on_demand_size(self):
        """Returns the approximate size of all on-demand artifacts stored across all versions."""
        all_content = (
            RepositoryContent.objects.filter(repository=self)
            .distinct("content")
            .values_list("content")
        )
        on_demand_ca = ContentArtifact.objects.filter(content__in=all_content, artifact=None)
        # Aggregate does not work with distinct("fields") so sum must be done manually
        ras = RemoteArtifact.objects.filter(
            content_artifact__in=on_demand_ca, size__isnull=False
        ).distinct("content_artifact")
        return sum(ras.values_list("size", flat=True))

    def on_new_version(self, version):
        """Called after a new repository version has been created.

        Subclasses are expected to override this to do useful things.

        Args:
            version: The new repository version.
        """
        pass

    def save(self, *args, **kwargs):
        """
        Saves Repository model and creates an initial repository version.

        Args:
            args (list): list of positional arguments for Model.save()
            kwargs (dict): dictionary of keyword arguments to pass to Model.save()
        """
        with transaction.atomic():
            adding = self._state.adding
            super().save(*args, **kwargs)
            if adding:
                self.create_initial_version()

                # lock the repository if it was created from within a running task
                task_id = Task.current_id()
                if task_id is None:
                    return

                repository_prn = Value(get_prn(instance=self))
                update_func = Func(
                    F("reserved_resources_record"), repository_prn, function="ARRAY_APPEND"
                )
                updated = Task.objects.filter(pk=task_id).update(
                    reserved_resources_record=update_func
                )
                if not updated:
                    raise RuntimeError(f"The repository '{self.name}' could not be locked")

    def create_initial_version(self):
        """
        Create an initial repository version (version 0).

        This method can be overriden by plugins if they require custom logic.
        """
        version = RepositoryVersion(repository=self, number=self.next_version, complete=True)
        self.next_version += 1
        self.save()
        version.save()

    def new_version(self, base_version=None):
        """
        Create a new RepositoryVersion for this Repository

        Creation of a RepositoryVersion should be done in a RQ Job.

        Args:
            repository (pulpcore.app.models.Repository): to create a new version of
            base_version (pulpcore.app.models.RepositoryVersion): an optional repository version
                whose content will be used as the set of content for the new version

        Returns:
            pulpcore.app.models.RepositoryVersion: The Created RepositoryVersion
        """
        with transaction.atomic():
            latest_version = self.versions.latest()
            if not latest_version.complete:
                latest_version.delete()

            version = RepositoryVersion(
                repository=self, number=int(self.next_version), base_version=base_version
            )
            version.save()

            if base_version:
                # first remove the content that isn't in the base version
                version.remove_content(version.content.exclude(pk__in=base_version.content))
                # now add any content that's in the base_version but not in version
                version.add_content(base_version.content.exclude(pk__in=version.content))

            if Task.current() and not self.user_hidden:
                resource = CreatedResource(content_object=version)
                resource.save()

            self.invalidate_cache()

            return version

    def initialize_new_version(self, new_version):
        """
        Initialize the new RepositoryVersion with plugin-provided code.

        This method should be overridden by plugin writers for an opportunity for plugin input. This
        method is intended to be called with the incomplete
        [pulpcore.app.models.RepositoryVersion][] to validate or modify the content.

        This method does not adjust the value of complete, or save the `RepositoryVersion` itself.
        Its intent is to allow the plugin writer an opportunity for plugin input before any other
        actions performed on the new `RepositoryVersion`.

        Args:
            new_version (pulpcore.app.models.RepositoryVersion): The incomplete RepositoryVersion to
                finalize.

        """
        pass

    def finalize_new_version(self, new_version):
        """
        Finalize the incomplete RepositoryVersion with plugin-provided code.

        This method should be overridden by plugin writers for an opportunity for plugin input. This
        method is intended to be called with the incomplete
        [pulpcore.app.models.RepositoryVersion][] to validate or modify the content.

        This method does not adjust the value of complete, or save the `RepositoryVersion` itself.
        Its intent is to allow the plugin writer an opportunity for plugin input before pulpcore
        marks the `RepositoryVersion` as complete.

        Args:
            new_version (pulpcore.app.models.RepositoryVersion): The incomplete RepositoryVersion to
                finalize.

        Returns:

        """
        pass

    def latest_version(self):
        """
        Get the latest RepositoryVersion on a repository

        Args:
            repository (pulpcore.app.models.Repository): to get the latest version of

        Returns:
            pulpcore.app.models.RepositoryVersion: The latest RepositoryVersion

        """
        with suppress(RepositoryVersion.DoesNotExist):
            model = self.versions.complete().latest()
            return model

    async def alatest_version(self):
        """
        Get the latest RepositoryVersion on a repository asynchronously

        Args:
            repository (pulpcore.app.models.Repository): to get the latest version of

        Returns:
            pulpcore.app.models.RepositoryVersion: The latest RepositoryVersion

        """
        with suppress(RepositoryVersion.DoesNotExist):
            model = await self.versions.complete().alatest()
            return model

    def natural_key(self):
        """
        Get the model's natural key.

        :return: The model's natural key.
        :rtype: tuple
        """
        return (self.name,)

    @staticmethod
    def on_demand_artifacts_for_version(version):
        """
        Returns the remote artifacts of on-demand content for a repository version.

        Provides a method that plugins can override since RepositoryVersions aren't typed.
        Note: this only returns remote artifacts that have a non-null size.

        Args:
            version (pulpcore.app.models.RepositoryVersion): to get the remote artifacts for.
        Returns:
            django.db.models.QuerySet: The remote artifacts that are contained within this version.
        """
        on_demand_ca = ContentArtifact.objects.filter(content__in=version.content, artifact=None)
        return RemoteArtifact.objects.filter(content_artifact__in=on_demand_ca, size__isnull=False)

    @staticmethod
    def artifacts_for_version(version):
        """
        Return the artifacts for a repository version.

        Provides a method that plugins can override since RepositoryVersions aren't typed.

        Args:
            version (pulpcore.app.models.RepositoryVersion): to get the artifacts for

        Returns:
            django.db.models.QuerySet: The artifacts that are contained within this version.
        """
        return Artifact.objects.filter(content__pk__in=version.content)

    def protected_versions(self):
        """
        Return repository versions that are protected.

        A protected version is one that is being served by a distro directly or via publication.

        Returns:
            django.db.models.QuerySet: Repo versions which are protected.
        """
        from .publication import Distribution, Publication

        # find all repo versions set on a distribution
        qs = self.versions.filter(pk__in=Distribution.objects.values_list("repository_version_id"))

        # find all repo versions with publications set on a distribution
        qs |= self.versions.filter(
            publication__pk__in=Distribution.objects.values_list("publication_id")
        )

        # Protect repo versions of distributed checkpoint publications.
        if Distribution.objects.filter(repository=self.pk, checkpoint=True).exists():
            qs |= self.versions.filter(
                publication__pk__in=Publication.objects.filter(checkpoint=True).values_list(
                    "pulp_id"
                )
            )

        if distro := Distribution.objects.filter(repository=self.pk, checkpoint=False).first():
            if distro.detail_model().SERVE_FROM_PUBLICATION:
                # if the distro serves publications, protect the latest published repo version
                version = self.versions.filter(
                    pk__in=Publication.objects.filter(complete=True).values_list(
                        "repository_version_id"
                    )
                ).last()
            else:
                # if the distro does not serve publications, use the latest repo version
                version = self.latest_version()

            if version:
                qs |= self.versions.filter(pk=version.pk)

        return qs.distinct()

    def pull_through_add_content(self, content_artifact):
        """
        Dispatch a task to add the passed in content_artifact from the content app's pull-through
        feature to this repository.

        Defaults to adding the associated content of the passed in content_artifact to the
        repository. Plugins should overwrite this method if more complex behavior is necessary, i.e.
        adding multiple associated content units in the same task.

        Args:
            content_artifact (pulpcore.app.models.ContentArtifact): the content artifact to add

        Returns:
            Optional(Task): Returns the dispatched task or None if nothing was done
        """
        cpk = content_artifact.content_id
        already_present = RepositoryContent.objects.filter(
            content__pk=cpk, repository=self, version_removed__isnull=True
        )
        if not cpk or already_present.exists():
            return None

        from pulpcore.plugin.tasking import dispatch, add_and_remove

        body = {"repository_pk": self.pk, "add_content_units": [cpk], "remove_content_units": []}
        return dispatch(add_and_remove, kwargs=body, exclusive_resources=[self], immediate=True)

    @hook(AFTER_UPDATE, when="retain_repo_versions", has_changed=True)
    def _cleanup_old_versions_hook(self):
        # Do not attempt to clean up anything, while there is a transaction involving repo versions
        # still in flight.
        transaction.on_commit(self.cleanup_old_versions)

    def cleanup_old_versions(self):
        """Cleanup old repository versions based on retain_repo_versions."""
        # I am still curious how, but it was reported that this state can happen in day to day
        # operations but its easy to reproduce manually in the pulpcore shell:
        # https://github.com/pulp/pulpcore/issues/2268
        if self.versions.filter(complete=False).exists():
            raise RuntimeError(
                _("Attempt to cleanup old versions, while a new version is in flight.")
            )
        if self.retain_repo_versions:
            # Consider only completed versions that aren't protected for cleanup
            versions = self.versions.complete().exclude(pk__in=self.protected_versions())
            for version in versions.order_by("-number")[self.retain_repo_versions :]:
                _logger.info(
                    "Deleting repository version {} due to version retention limit.".format(version)
                )
                version.delete()

    def delete(self, **kwargs):
        """
        Delete the repository.

        Args:
            **kwargs (dict): Delete options.
        """
        from .publication import Publication, PublishedArtifact  # circular import avoidance

        # The purpose is to avoid the memory spike caused by the deletion of an object at
        # the apex of a large tree of cascading deletes. As per the Django documentation [0],
        # cascading deletes are handled by Django and require objects to be loaded into
        # memory. If the tree of objects is sufficiently large, this can result in a fatal
        # memory spike.
        #
        # Therefore, we manually delete the objects which we know we have many thousands of
        # first, to make the cascade delete managable.
        #
        # [0] https://docs.djangoproject.com/en/4.2/ref/models/querysets/#delete
        with transaction.atomic():
            repo_versions = RepositoryVersion.objects.filter(repository=self)
            repo_contents = RepositoryContent.objects.filter(repository=self)
            publications = Publication.objects.filter(
                repository_version__in=repo_versions.values_list("pk", flat=True)
            )
            published_artifacts = PublishedArtifact.objects.filter(
                publication__in=publications.values_list("pk", flat=True)
            )

            # PublishedArtifact and RepositoryContent are the two most numerous object types
            # PublishedMetadata would be trickier to delete because it's a Content subclass
            # that is ignored by orphan cleanup, so to delete those in this way would require
            # manual intervention
            published_artifacts._raw_delete(published_artifacts.db)
            repo_contents._raw_delete(repo_contents.db)

            # Anything not deleted manually above will be caught up in Django cascade. Deleting
            # those ojects manually should keep this operation from being too brutal.
            return super().delete(**kwargs)

    @hook(BEFORE_DELETE)
    def invalidate_cache(self, everything=False):
        """Invalidates the cache if repository is present."""
        if settings.CACHE_ENABLED:
            distributions = self.distributions.all()
            if everything:
                from .publication import Distribution, Publication

                versions = self.versions.all()
                pubs = Publication.objects.filter(repository_version__in=versions, complete=True)
                distributions |= Distribution.objects.filter(publication__in=pubs)
                distributions |= Distribution.objects.filter(repository_version__in=versions)
            if distributions.exists():
                base_paths = distributions.values_list("base_path", flat=True)
                if base_paths:
                    Cache().delete(base_key=cache_key(base_paths))
                # Could do preloading here for immediate artifacts with artifacts_for_version


class Remote(MasterModel):
    """
    A remote source for content.

    This is meant to be subclassed by plugin authors as an opportunity to provide plugin-specific
    persistent data attributes for a plugin remote subclass.

    This object is a Django model that inherits from [pulpcore.app.models.Remote][] which
    provides the platform persistent attributes for a remote object. Plugin authors can add
    additional persistent remote data by subclassing this object and adding Django fields. We
    defer to the Django docs on extending this model definition with additional fields.

    Validation of the remote is done at the API level by a plugin defined subclass of
    [pulpcore.plugin.serializers.repository.RemoteSerializer][].

    Fields:

        name (models.TextField): The remote name.
        pulp_labels (HStoreField): Dictionary of string values.
        url (models.TextField): The URL of an external content source.
        ca_cert (models.TextField): A PEM encoded CA certificate used to validate the
            server certificate presented by the external source.
        client_cert (models.TextField): A PEM encoded client certificate used
            for authentication.
        client_key (models.TextField): A PEM encoded private key used for authentication.
        tls_validation (models.BooleanField): If True, TLS peer validation must be performed.
        proxy_url (models.TextField): The optional proxy URL.
            Format: scheme://host:port
        proxy_username (models.TextField): The optional username to authenticate with the proxy.
        proxy_password (models.TextField): The optional password to authenticate with the proxy.
        username (models.TextField): The username to be used for authentication when syncing.
        password (models.TextField): The password to be used for authentication when syncing.
        download_concurrency (models.PositiveIntegerField): Total number of
            simultaneous connections allowed to any remote during a sync.
        policy (models.TextField): The policy to use when downloading content.
        total_timeout (models.FloatField): Value for aiohttp.ClientTimeout.total on connections
        connect_timeout (models.FloatField): Value for aiohttp.ClientTimeout.connect
        sock_connect_timeout (models.FloatField): Value for aiohttp.ClientTimeout.sock_connect
        sock_read_timeout (models.FloatField): Value for aiohttp.ClientTimeout.sock_read
        headers (models.JSONField): Headers set on the aiohttp.ClientSession
        rate_limit (models.IntegerField): Limits requests per second for each concurrent downloader

    Relations:

        pulp_domain (models.ForeignKey): The domain the Remote is a part of.
    """

    TYPE = "remote"

    # Constants for the ChoiceField 'policy'
    IMMEDIATE = "immediate"
    ON_DEMAND = "on_demand"
    STREAMED = "streamed"

    DEFAULT_DOWNLOAD_CONCURRENCY = 10
    DEFAULT_MAX_RETRIES = 3

    POLICY_CHOICES = (
        (IMMEDIATE, "When syncing, download all metadata and content now."),
        (
            ON_DEMAND,
            "When syncing, download metadata, but do not download content now. Instead, "
            "download content as clients request it, and save it in Pulp to be served for "
            "future client requests.",
        ),
        (
            STREAMED,
            "When syncing, download metadata, but do not download content now. Instead,"
            "download content as clients request it, but never save it in Pulp. This causes "
            "future requests for that same content to have to be downloaded again.",
        ),
    )

    name = models.TextField(db_index=True)
    pulp_labels = HStoreField(default=dict)

    url = models.TextField()

    ca_cert = models.TextField(null=True)
    client_cert = models.TextField(null=True)
    client_key = EncryptedTextField(null=True)
    tls_validation = models.BooleanField(default=True)

    username = EncryptedTextField(null=True)
    password = EncryptedTextField(null=True)

    proxy_url = models.TextField(null=True)
    proxy_username = EncryptedTextField(null=True)
    proxy_password = EncryptedTextField(null=True)

    download_concurrency = models.PositiveIntegerField(
        null=True, validators=[MinValueValidator(1, "Download concurrency must be at least 1")]
    )
    max_retries = models.PositiveIntegerField(null=True)
    policy = models.TextField(choices=POLICY_CHOICES, default=IMMEDIATE)

    total_timeout = models.FloatField(
        null=True, validators=[MinValueValidator(0.0, "Timeout must be >= 0")]
    )
    connect_timeout = models.FloatField(
        null=True, validators=[MinValueValidator(0.0, "Timeout must be >= 0")]
    )
    sock_connect_timeout = models.FloatField(
        null=True, validators=[MinValueValidator(0.0, "Timeout must be >= 0")]
    )
    sock_read_timeout = models.FloatField(
        null=True, validators=[MinValueValidator(0.0, "Timeout must be >= 0")]
    )
    headers = models.JSONField(blank=True, null=True)
    rate_limit = models.IntegerField(null=True)

    pulp_domain = models.ForeignKey("Domain", default=get_domain_pk, on_delete=models.PROTECT)

    @property
    def download_factory(self):
        """
        Return the DownloaderFactory which can be used to generate asyncio capable downloaders.

        Upon first access, the DownloaderFactory is instantiated and saved internally.

        Plugin writers are expected to override when additional configuration of the
        DownloaderFactory is needed.

        Returns:
            DownloadFactory: The instantiated DownloaderFactory to be used by
                get_downloader().
        """
        try:
            return self._download_factory
        except AttributeError:
            self._download_factory = DownloaderFactory(self)
            return self._download_factory

    @property
    def download_throttler(self):
        """
        Return the Throttler which can be used to rate limit downloaders.

        Upon first access, the Throttler is instantiated and saved internally.
        Plugin writers are expected to override when additional configuration of the
        DownloaderFactory is needed.

        Returns:
            Throttler: The instantiated Throttler to be used by get_downloader()

        """
        try:
            return self._download_throttler
        except AttributeError:
            if self.rate_limit:
                self._download_throttler = Throttler(rate_limit=self.rate_limit)
                return self._download_throttler

    def get_downloader(self, remote_artifact=None, url=None, download_factory=None, **kwargs):
        """
        Get a downloader from either a RemoteArtifact or URL that is configured with this Remote.

        This method accepts either `remote_artifact` or `url` but not both. At least one is
        required. If neither or both are passed a ValueError is raised.

        Plugin writers are expected to override when additional configuration is needed or when
        another class of download is required.

        Args:
            remote_artifact (pulpcore.app.models.RemoteArtifact) The RemoteArtifact to
                download.
            url (str): The URL to download.
            download_factory (pulpcore.plugin.download.DownloadFactory) The download
                factory to be used.
            kwargs (dict): This accepts the parameters of
                [pulpcore.plugin.download.BaseDownloader][].

        Raises:
            ValueError: If neither remote_artifact and url are passed, or if both are passed.

        Returns:
            subclass of [pulpcore.plugin.download.BaseDownloader][]: A downloader that
            is configured with the remote settings.
        """
        if remote_artifact and url:
            raise ValueError(_("get_downloader() cannot accept both 'remote_artifact' and 'url'."))
        if remote_artifact is None and url is None:
            raise ValueError(_("get_downloader() requires either 'remote_artifact' and 'url'."))
        if remote_artifact:
            url = remote_artifact.url
            expected_digests = {}
            for digest_name in ALL_KNOWN_CONTENT_CHECKSUMS:
                digest_value = getattr(remote_artifact, digest_name)
                if digest_value:
                    expected_digests[digest_name] = digest_value
            if expected_digests:
                kwargs["expected_digests"] = expected_digests
            if remote_artifact.size:
                kwargs["expected_size"] = remote_artifact.size
        if download_factory is None:
            download_factory = self.download_factory
        return download_factory.build(url, **kwargs)

    def get_remote_artifact_url(self, relative_path=None, request=None):
        """
        Get the full URL for a RemoteArtifact from relative path and request.

        This method returns the URL for a RemoteArtifact by concatenating the Remote's url and the
        relative path. Plugin writers are expected to override this method when a more complex
        algorithm is needed to determine the full URL.

        Args:
            relative_path (str): The relative path of a RemoteArtifact
            request (aiohttp.web.Request): The request object for this relative path

        Raises:
            ValueError: If relative_path starts with a '/'.

        Returns:
            str: A URL for a RemoteArtifact available at the Remote.
        """
        if path.isabs(relative_path):
            raise ValueError(_("Relative path can't start with '/'. {0}").format(relative_path))
        return path.join(self.url, relative_path)

    def get_remote_artifact_content_type(self, relative_path=None):
        """
        Get the type of content that should be available at the relative path.

        Plugin writers are expected to implement this method. This method can return None if the
        relative path is for metadata that should only be streamed from the remote and not saved.

        Args:
            relative_path (str): The relative path of a RemoteArtifact

        Returns:
            Optional[Class]: The optional Class of the content type that should be available at the
                relative path.
        """
        raise NotImplementedError()

    @hook(BEFORE_DELETE)
    def invalidate_cache(self):
        """Invalidates the cache if remote is present."""
        if settings.CACHE_ENABLED:
            base_paths = self.distribution_set.values_list("base_path", flat=True)
            if base_paths:
                Cache().delete(base_key=cache_key(base_paths))

    class Meta:
        default_related_name = "remotes"
        unique_together = ("name", "pulp_domain")


class RepositoryContent(BaseModel):
    """
    Association between a repository and its contained content.

    Fields:

        created (models.DatetimeField): When the association was created.

    Relations:

        content (models.ForeignKey): The associated content.
        repository (models.ForeignKey): The associated repository.
        version_added (models.ForeignKey): The RepositoryVersion which added the referenced
            Content.
        version_removed (models.ForeignKey): The RepositoryVersion which removed the referenced
            Content.
    """

    # Content can only be removed once it's no longer referenced by any repository
    content = models.ForeignKey(
        "Content", on_delete=models.PROTECT, related_name="version_memberships"
    )
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    # version_added and version_removed need to be properly handled in _squash before the version
    # can be deleted
    version_added = models.ForeignKey(
        "RepositoryVersion", related_name="added_memberships", on_delete=models.RESTRICT
    )
    version_removed = models.ForeignKey(
        "RepositoryVersion",
        null=True,
        related_name="removed_memberships",
        on_delete=models.RESTRICT,
    )

    class Meta:
        unique_together = (
            ("repository", "content", "version_added"),
            ("repository", "content", "version_removed"),
        )


class RepositoryVersionQuerySet(models.QuerySet):
    """A queryset that provides repository version filtering methods."""

    def complete(self):
        return self.filter(complete=True)

    def with_content(self, content):
        """
        Filters repository versions that contain the provided content units.

        Args:
            content (django.db.models.QuerySet): query of content

        Returns:
            django.db.models.QuerySet: Repository versions which contains content.
        """
        # TODO: Evaluate if this can be optimized with content_ids field
        query = models.Q(pk__in=[])
        repo_content = RepositoryContent.objects.filter(content__pk__in=content)

        for rc in repo_content.iterator():
            filter = models.Q(
                repository__pk=rc.repository.pk,
                number__gte=rc.version_added.number,
            )
            if rc.version_removed:
                filter &= models.Q(number__lt=rc.version_removed.number)

            query |= filter

        return self.filter(query)


class RepositoryVersion(BaseModel):
    """
    A version of a repository's content set.

    Plugin Writers are strongly encouraged to use RepositoryVersion as a context manager to provide
    transactional safety, working directory set up, plugin finalization, and cleaning up the
    database on failures.

    Examples::

        with repository.new_version(repository) as new_version:
            new_version.add_content(content_q)
            new_version.remove_content(content_q)

    Fields:

        number (models.PositiveIntegerField): A positive integer that uniquely identifies a version
            of a specific repository. Each new version for a repo should have this field set to
            1 + the most recent version.
        complete (models.BooleanField): If true, the RepositoryVersion is visible. This field is set
            to true when the task that creates the RepositoryVersion is complete.

    Relations:

        repository (models.ForeignKey): The associated repository.
        base_version (models.ForeignKey): The repository version this was created from.
    """

    objects = RepositoryVersionQuerySet.as_manager()

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    number = models.PositiveIntegerField(db_index=True)
    complete = models.BooleanField(db_index=True, default=False)
    base_version = models.ForeignKey("RepositoryVersion", null=True, on_delete=models.SET_NULL)
    info = models.JSONField(default=dict)
    content_ids = ArrayField(models.UUIDField(), default=None, null=True)

    class Meta:
        default_related_name = "versions"
        unique_together = ("repository", "number")
        get_latest_by = "number"
        ordering = ("number",)

    def _content_relationships(self):
        """
        Returns a set of repository_content for a repository version

        Returns:
            django.db.models.QuerySet: The repository_content that is contained within this version.
        """
        return RepositoryContent.objects.filter(
            repository_id=self.repository_id, version_added__number__lte=self.number
        ).exclude(version_removed__number__lte=self.number)

    def _get_content_ids(self):
        """
        Returns the content ids for a repository version
        """
        if self.content_ids is not None:
            return self.content_ids
        return self._content_relationships().values_list("content_id", flat=True)

    @hook(BEFORE_CREATE)
    def set_content_ids(self):
        """
        Sets the content ids for the new repository version based on the previous version.
        """
        try:
            previous = self.previous()
        except self.DoesNotExist:
            pass
        else:
            if previous.content_ids is not None:
                self.content_ids = previous.content_ids
        if self.content_ids is None:
            self.content_ids = list(
                self._content_relationships().values_list("content_id", flat=True)
            )

    def get_content(self, content_qs=None):
        """
        Returns a set of content for a repository version

        Args:
            content_qs (django.db.models.QuerySet): The queryset for Content that will be
                restricted further to the content present in this repository version. If not given,
                ``Content.objects.all()`` is used (to return over all content types present in the
                repository version).

        Returns:
            django.db.models.QuerySet: The content that is contained within this version.

        Examples:
            >>> repository_version = ...
            >>>
            >>> # Return a queryset of File objects in the repository
            >>> repository_version.get_content(content_qs=File.objects)):
        """

        if content_qs is None:
            content_qs = Content.objects

        content_ids = self._get_content_ids()
        if isinstance(content_ids, list) and len(content_ids) >= 65535:
            # Workaround for PostgreSQL's limit on the number of parameters in a query
            content_ids = (
                RepositoryVersion.objects.filter(pk=self.pk)
                .annotate(cids=Func(F("content_ids"), function="unnest"))
                .values_list("cids", flat=True)
            )
        return content_qs.filter(pk__in=content_ids)

    @property
    def content(self):
        """
        Returns a set of content for a repository version

        Returns:
            django.db.models.QuerySet: The content that is contained within this version.

        Examples:
            >>> repository_version = ...
            >>>
            >>> for content in repository_version.content:
            >>>     content = content.cast()  # optional downcast.
            >>>     ...
            >>>
            >>> for content in FileContent.objects.filter(pk__in=repository_version.content):
            >>>     ...
            >>>
        """

        return self.get_content()

    def content_batch_qs(self, content_qs=None, order_by_params=("pk",), batch_size=1000):
        """
        Generate content batches to efficiently iterate over all content.

        Generates query sets that span the `content_qs` content of the repository
        version. Each yielded query set evaluates to at most `batch_size` content records.
        This is useful to limit the memory footprint when iterating over all content of
        a repository version.

        .. note::

            * This generator is not safe against changes (i.e. add/remove content) during
              the iteration!

            * As the method uses slices internally, the queryset must be ordered to yield
              stable results. By default, it is ordered by primary key.

        Args:
            content_qs (django.db.models.QuerySet) The queryset for Content that will be
                restricted further to the content present in this repository version. If not given,
                ``Content.objects.all()`` is used (to iterate over all content present in the
                repository version). A plugin may want to use a specific subclass of
                [pulpcore.plugin.models.Content][] or use e.g. ``filter()`` to select
                a subset of the repository version's content.
            order_by_params (tuple of str): The parameters for the ``order_by`` clause
                for the content. The Default is ``("pk",)``. This needs to
                specify a stable order. For example, if you want to iterate by
                decreasing creation time stamps use ``("-pulp_created", "pk")`` to
                ensure that content records are still sorted by primary key even
                if their creation timestamp happens to be equal.
            batch_size (int): The maximum batch size.

        Yields:
            [django.db.models.QuerySet][]: A QuerySet representing a slice of the content.

        Example:
            The following code could be used to loop over all ``FileContent`` in
            ``repository_version``. It prefetches the related
            [pulpcore.plugin.models.ContentArtifact][] instances for every batch::

                repository_version = ...

                batch_generator = repository_version.content_batch_qs(
                    content_class=FileContent.objects.all()
                )
                for content_batch_qs in batch_generator:
                    content_batch_qs.prefetch_related("contentartifact_set")
                    for content in content_batch_qs:
                        ...

        """
        version_content_qs = self.get_content(content_qs).order_by(*order_by_params)
        yield from batch_qs(version_content_qs, batch_size=batch_size)

    @property
    def artifacts(self):
        """
        Returns a set of artifacts for a repository version.

        Returns:
            django.db.models.QuerySet: The artifacts that are contained within this version.
        """
        return self.repository.cast().artifacts_for_version(self)

    @property
    def on_demand_artifacts(self):
        return self.repository.cast().on_demand_artifacts_for_version(self)

    @property
    def disk_size(self):
        """Returns the size on disk of all the artifacts in this repository version."""
        return self.artifacts.distinct().aggregate(size=models.Sum("size", default=0))["size"]

    @property
    def on_demand_size(self):
        """Returns the size of on-demand artifacts in this repository version."""
        ras = self.on_demand_artifacts.distinct("content_artifact")
        return sum(ras.values_list("size", flat=True))

    def added(self, base_version=None):
        """
        Args:
            base_version (pulpcore.app.models.RepositoryVersion): an optional repository version

        Returns:
            QuerySet: The Content objects that were added by this version.
        """
        if not base_version:
            return Content.objects.filter(version_memberships__version_added=self)

        return Content.objects.filter(pk__in=self._get_content_ids()).exclude(
            pk__in=base_version._get_content_ids()
        )

    def removed(self, base_version=None):
        """
        Args:
            base_version (pulpcore.app.models.RepositoryVersion): an optional repository version

        Returns:
            QuerySet: The Content objects that were removed by this version.
        """
        if not base_version:
            return Content.objects.filter(version_memberships__version_removed=self)

        return Content.objects.filter(pk__in=base_version._get_content_ids()).exclude(
            pk__in=self._get_content_ids()
        )

    def contains(self, content):
        """
        Check whether a content exists in this repository version's set of content

        Returns:
            bool: True if the repository version contains the content, False otherwise
        """
        if self.content_ids is not None:
            return content.pk in self.content_ids
        return self.content.filter(pk=content.pk).exists()

    def add_content(self, content):
        """
        Add a content unit to this version.

        Args:
           content (django.db.models.QuerySet): Set of Content to add

        Raise:
            pulpcore.exception.ResourceImmutableError: if add_content is called on a
                complete RepositoryVersion
        """

        if self.complete:
            raise ResourceImmutableError(self)

        assert (
            not Content.objects.filter(pk__in=content)
            .exclude(pulp_domain_id=get_domain_pk())
            .exists()
        )
        repo_content = []
        to_add = set(content.values_list("pk", flat=True)) - set(self._get_content_ids())
        with transaction.atomic():
            if to_add:
                self.content_ids += list(to_add)
                self.save()

            # Normalize representation if content has already been removed in this version and
            # is re-added: Undo removal by setting version_removed to None.
            for removed in batch_qs(self.removed().order_by("pk").values_list("pk", flat=True)):
                to_readd = to_add.intersection(set(removed))
                if to_readd:
                    RepositoryContent.objects.filter(
                        content__in=to_readd, repository=self.repository, version_removed=self
                    ).update(version_removed=None)
                    to_add = to_add - to_readd

            for content_pk in to_add:
                repo_content.append(
                    RepositoryContent(
                        repository=self.repository, content_id=content_pk, version_added=self
                    )
                )

            RepositoryContent.objects.bulk_create(repo_content)

    def remove_content(self, content):
        """
        Remove content from the repository.

        Args:
            content (django.db.models.QuerySet): Set of Content to remove

        Raise:
            pulpcore.exception.ResourceImmutableError: if remove_content is called on a
                complete RepositoryVersion
        """

        if self.complete:
            raise ResourceImmutableError(self)

        if not content or not content.count():
            return
        assert (
            not Content.objects.filter(pk__in=content)
            .exclude(pulp_domain_id=get_domain_pk())
            .exists()
        )
        content_ids = set(self._get_content_ids())
        to_remove = set(content.values_list("pk", flat=True))
        with transaction.atomic():
            if to_remove:
                self.content_ids = list(content_ids - to_remove)
                self.save()

            # Normalize representation if content has already been added in this version.
            # Undo addition by deleting the RepositoryContent.
            RepositoryContent.objects.filter(
                repository=self.repository,
                content_id__in=content,
                version_added=self,
                version_removed=None,
            ).delete()

            q_set = RepositoryContent.objects.filter(
                repository=self.repository, content_id__in=content, version_removed=None
            )
            q_set.update(version_removed=self)

    def set_content(self, content):
        """
        Sets the repo version content by calling remove_content() then add_content().

        Args:
            content (django.db.models.QuerySet): Set of desired content

        Raise:
            pulpcore.exception.ResourceImmutableError: if set_content is called on a
                complete RepositoryVersion
        """
        self.remove_content(self.content.exclude(pk__in=content))
        self.add_content(content.exclude(pk__in=self.content))

    def next(self):
        """
        Returns:
            [pulpcore.app.models.RepositoryVersion][]: The next complete RepositoryVersion
            for the same repository.

        Raises:
            [RepositoryVersion.DoesNotExist][]: if there is not a RepositoryVersion for the same
                repository and with a higher "number".
        """
        try:
            return (
                self.repository.versions.complete()
                .filter(number__gt=self.number)
                .order_by("number")[0]
            )
        except IndexError:
            raise self.DoesNotExist

    def previous(self):
        """
        Returns:
            pulpcore.app.models.RepositoryVersion: The previous complete RepositoryVersion for the
                same repository.

        Raises:
            RepositoryVersion.DoesNotExist: if there is not a RepositoryVersion for the same
                repository and with a lower "number".
        """
        try:
            return (
                self.repository.versions.complete()
                .filter(number__lt=self.number)
                .order_by("-number")[0]
            )
        except IndexError:
            raise self.DoesNotExist

    def _squash(self, repo_relations, next_version):
        """
        Squash a complete repo version into the next version
        """
        # delete any relationships added in the version being deleted and removed in the next one.
        repo_relations.filter(version_added=self, version_removed=next_version).delete()

        # If the same content is deleted in version, but added back in next_version then:
        # - set version_removed field in relation to version_removed of the relation adding
        #   the content in next version because the content can be removed again after the
        #   next_version
        # - and remove relation adding the content in next_version
        content_added = repo_relations.filter(version_added=next_version).values_list("content_id")

        content_removed_and_readded = repo_relations.filter(
            version_removed=self, content_id__in=content_added
        ).values_list("content_id")

        repo_contents_readded_in_next_version = repo_relations.filter(
            version_added=next_version, content_id__in=content_removed_and_readded
        )

        # Since the readded contents can be removed again by any subsequent version after the
        # next version. Get the mapping of readded contents and their versions removed to use
        # later. The version removed id will be None if a content is not removed.
        version_removed_id_content_id_map = defaultdict(list)
        for readded_repo_content in repo_contents_readded_in_next_version.iterator():
            version_removed_id_content_id_map[readded_repo_content.version_removed_id].append(
                readded_repo_content.content_id
            )

        repo_contents_readded_in_next_version.delete()

        # Update the version removed of the readded contents
        for version_removed_id, content_ids in version_removed_id_content_id_map.items():
            repo_relations.filter(version_removed=self, content_id__in=content_ids).update(
                version_removed_id=version_removed_id
            )

        # "squash" by moving other additions and removals forward to the next version
        repo_relations.filter(version_added=self).update(version_added=next_version)
        repo_relations.filter(version_removed=self).update(version_removed=next_version)

        # Update next version's counts as they have been modified
        next_version._compute_counts()

    @hook(BEFORE_DELETE)
    def check_protected(self):
        """Check if a repo version is protected before trying to delete it."""
        if self in self.repository.protected_versions():
            raise Exception(PROTECTED_REPO_VERSION_MESSAGE)

    def delete(self, **kwargs):
        """
        Deletes a RepositoryVersion

        If RepositoryVersion is complete and has a successor, squash RepositoryContent changes into
        the successor. If version is incomplete, delete and and clean up RepositoryContent,
        CreatedResource, and Repository objects.

        Deletion of a complete RepositoryVersion should be done in a task.
        """
        if self.complete:
            if self.repository.versions.complete().count() <= 1:
                raise APIException(_("Attempt to delete the last remaining version."))
            if settings.CACHE_ENABLED:
                base_paths = self.distribution_set.values_list("base_path", flat=True)
                if base_paths:
                    Cache().delete(base_key=cache_key(base_paths))

            # Handle the manipulation of the repository version content and its final deletion in
            # the same transaction.
            with transaction.atomic():
                repo_relations = RepositoryContent.objects.filter(
                    repository=self.repository
                ).select_for_update()
                try:
                    next_version = self.next()
                    self._squash(repo_relations, next_version)

                except RepositoryVersion.DoesNotExist:
                    # version is the latest version so simply update repo contents
                    # and delete the version
                    repo_relations.filter(version_added=self).delete()
                    repo_relations.filter(version_removed=self).update(version_removed=None)

                if repo_relations.filter(Q(version_added=self) | Q(version_removed=self)).exists():
                    raise RuntimeError(
                        _("Some repo relations of this version were not translated.")
                    )
                super().delete(**kwargs)

        else:
            with transaction.atomic():
                RepositoryContent.objects.filter(version_added=self).delete()
                RepositoryContent.objects.filter(version_removed=self).update(version_removed=None)
                CreatedResource.objects.filter(object_id=self.pk).delete()
                super().delete(**kwargs)

    def _compute_counts(self):
        """
        Compute and save content unit counts by type.

        Count records are stored as [pulpcore.app.models.RepositoryVersionContentDetails][].
        This method deletes existing [pulpcore.app.models.RepositoryVersionContentDetails][]
        objects and makes new ones with each call.
        """
        with transaction.atomic():
            RepositoryVersionContentDetails.objects.filter(repository_version=self).delete()
            counts_list = []
            for value, name in RepositoryVersionContentDetails.COUNT_TYPE_CHOICES:
                if value == RepositoryVersionContentDetails.ADDED:
                    qs = self.added()
                elif value == RepositoryVersionContentDetails.PRESENT:
                    qs = self.content
                elif value == RepositoryVersionContentDetails.REMOVED:
                    qs = self.removed()
                annotated = qs.values("pulp_type").annotate(count=models.Count("pulp_type"))
                for item in annotated:
                    count_obj = RepositoryVersionContentDetails(
                        content_type=item["pulp_type"],
                        repository_version=self,
                        count=item["count"],
                        count_type=value,
                    )
                    counts_list.append(count_obj)
            RepositoryVersionContentDetails.objects.bulk_create(counts_list)

    def __enter__(self):
        """
        Create the repository version

        Returns:
            RepositoryVersion: self
        """
        if self.complete:
            raise RuntimeError(
                _("This Repository version is complete. It cannot be modified further.")
            )
        repository = self.repository.cast()
        repository.initialize_new_version(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Finalize and save the RepositoryVersion if no errors are raised, delete it if not
        """
        if exc_value:
            self.delete()
        else:
            try:
                repository = self.repository.cast()
                repository.finalize_new_version(self)
                no_change = not self.added() and not self.removed()
                if no_change:
                    self.delete()
                else:
                    content_types_seen = set(
                        self.content.values_list("pulp_type", flat=True).distinct()
                    )
                    content_types_supported = set(
                        ctype.get_pulp_type() for ctype in repository.CONTENT_TYPES
                    )

                    unsupported_types = content_types_seen - content_types_supported
                    if unsupported_types:
                        raise ValueError(
                            _("Saw unsupported content types {}").format(unsupported_types)
                        )

                    self.complete = True
                    self.repository.next_version = self.number + 1
                    with transaction.atomic():
                        self.repository.save()
                        self.save()
                        self._compute_counts()
                    self.repository.cleanup_old_versions()
                    repository.on_new_version(self)
            except Exception:
                self.delete()
                raise

    def __str__(self):
        return "<Repository: {}; Version: {}>".format(self.repository.name, self.number)


class RepositoryVersionContentDetails(models.Model):
    ADDED = "A"
    PRESENT = "P"
    REMOVED = "R"
    COUNT_TYPE_CHOICES = (
        (ADDED, "added"),
        (PRESENT, "present"),
        (REMOVED, "removed"),
    )

    count_type = models.TextField(choices=COUNT_TYPE_CHOICES)
    content_type = models.TextField()
    repository_version = models.ForeignKey(
        "RepositoryVersion", related_name="counts", on_delete=models.CASCADE
    )
    count = models.IntegerField()

    def get_content_href(self, request=None):
        """
        Generate URLs for the content types added, removed, or present in the RepositoryVersion.

        For each content type present in or removed from this RepositoryVersion, create the URL of
        the viewset of that variety of content along with a query parameter which filters it by
        presence in this RepositoryVersion summary.

        Args:
            obj (pulpcore.app.models.RepositoryVersion): The RepositoryVersion being serialized.
        Returns:
            dict: {<pulp_type>: <url>}
        """
        repository_model = Repository.get_model_for_pulp_type(
            self.repository_version.repository.pulp_type
        )
        ctypes = {c.get_pulp_type(): c for c in repository_model.CONTENT_TYPES}
        ctype_model = ctypes[self.content_type]
        ctype_view = get_view_name_for_model(ctype_model, "list")
        try:
            ctype_url = reverse(ctype_view, request=request)
        except django.urls.exceptions.NoReverseMatch:
            # We've hit a content type for which there is no viewset.
            # There's nothing we can do here, except to skip it.
            return

        repository_view = get_view_name_for_model(repository_model, "list")

        repository_url = reverse(repository_view, request=request)
        rv_href = (
            repository_url
            + str(self.repository_version.repository_id)
            + "/versions/{version}/".format(version=self.repository_version.number)
        )
        if self.count_type == self.ADDED:
            partial_url_str = "{base}?repository_version_added={rv_href}"
        elif self.count_type == self.PRESENT:
            partial_url_str = "{base}?repository_version={rv_href}"
        elif self.count_type == self.REMOVED:
            partial_url_str = "{base}?repository_version_removed={rv_href}"
        full_url = partial_url_str.format(base=ctype_url, rv_href=rv_href)
        return full_url

    content_href = property(get_content_href)
