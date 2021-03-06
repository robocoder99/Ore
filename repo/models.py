from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, UserManager, AnonymousUser
from django.core import validators
from django.core.mail import send_mail
from django.db import models
from django.utils.translation import ugettext_lazy as _t
from django.utils import timezone
from django.db.models.signals import post_save
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

from model_utils.managers import InheritanceManagerMixin, InheritanceQuerySetMixin
from model_utils.fields import StatusField
from model_utils import Choices
import reversion
import hashlib

from .regexs import *

Q = models.Q
F = models.F


def prefix_q(prefix, **kwargs):
    return Q(**{
        prefix + k: v for k, v in kwargs.items()
    })


class UserFilteringQuerySet(models.QuerySet):
    def as_user(self, user):
        return self.filter(self.model.is_visible_q('', user))


class UserFilteringInheritanceQuerySet(InheritanceQuerySetMixin, UserFilteringQuerySet):
    pass


UserFilteringManager = models.Manager.from_queryset(UserFilteringQuerySet)
class UserFilteringInheritanceManager(InheritanceManagerMixin, UserFilteringManager):
    def get_queryset(self):
        return UserFilteringInheritanceQuerySet(self.model, using=self._db)


@reversion.register
class Namespace(models.Model):

    STATUS = Choices('active', 'deleted')
    status = StatusField()

    name = models.CharField('name', max_length=32, unique=True,
                            validators=[
                                validators.RegexValidator(EXTENDED_NAME_REGEX, 'Enter a namespace organization name.', 'invalid')
                            ])

    objects = UserFilteringInheritanceManager() 

    @staticmethod
    def is_visible_q(prefix, user):
        if not user.is_authenticated():
            return prefix_q(prefix, status='active')

        return prefix_q(prefix, status='active') | prefix_q(prefix, repouser=user) | prefix_q(prefix, organization__teams__users=user)

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<Namespace %s>' % self.name

class RepoUserManager(UserManager):
    def _create_user(self, username, email, password, is_staff, is_superuser, **extra_fields):
        now = timezone.now()
        if not username:
            raise ValueError("The given username must be set")
        email = self.normalize_email(email)
        user = self.model(name=username, email=email, is_staff=is_staff, is_active=True, is_superuser=True, date_joined=now, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user


class RepoUser(AbstractBaseUser, PermissionsMixin, Namespace):

    # All taken from AbstractUser
    # name from Namespace
    email = models.EmailField('email', blank=True)
    is_staff = models.BooleanField('staff status', default=False,
                                   help_text='Designates whether the user can log into this admin '
                                             'site.')
    is_active = models.BooleanField('active', default=True,
                                    help_text='Designates whether this user should be treated as '
                                              'active. Unselect this instead of deleting accounts.')
    date_joined = models.DateTimeField(_t('creation date'), default=timezone.now)

    objects = RepoUserManager()

    USERNAME_FIELD = 'name'
    REQUIRED_FIELDS = ['email']

    @property
    def avatar(self):
        return "//www.gravatar.com/avatar/" + hashlib.md5(self.email.encode('UTF-8').strip().lower()).hexdigest() + "?d=mm"

    def get_short_name(self):
        return self.name

    def get_full_name(self):
        return self.name

    def email_user(self, subject, message, from_email=None, **kwargs):
        send_mail(subject, message, from_email, [self.email], **kwargs)

    def user_has_permission(self, user, perm_slug, project=None):
        if isinstance(user, AnonymousUser):
            return False
        return user == self

    def __str__(self):
        return self.name

    def __repr__(self):
        props = (['staff'] if self.is_staff else []) + (['active'] if self.is_active else [])
        return '<RepoUser %s <%s> [%s]>' % (self.name, self.email, ' '.join(props))

reversion.register(RepoUser, follow=['namespace_ptr'])


def organization_avatar_upload(instance, filename):
    import posixpath
    import uuid
    _, fileext = posixpath.splitext(filename)
    final_filename = uuid.uuid4().hex + fileext
    return posixpath.join('avatars', 'organization', instance.name, final_filename)


class Organization(Namespace):

    avatar_image = models.ImageField(upload_to=organization_avatar_upload, blank=True, null=True, default=None)

    @property
    def avatar(self):
        if self.avatar_image:
            return self.avatar_image.url
        return "//www.gravatar.com/avatar/mysteryman?f=y&d=mm"

    def user_has_permission(self, user, perm_slug, project=None):
        if isinstance(user, AnonymousUser):
            return False

        ownerships = user.__dict__.setdefault('_organization_ownerships', dict())
        permissions = user.__dict__.setdefault('_organization_permissions', dict())
        if ownerships.get(self.id) is None:
            qs = self.teams.filter(users=user)
            qs = qs.filter(Q(is_all_projects=True) | Q(projects=project))
            if qs.filter(is_owner_team=True).count():
                ownerships[self.id] = True
            else:
                permissions[self.id] = qs.values_list('permissions__slug', flat=True)

        if self.id in ownerships:
            return True
        if perm_slug in permissions.get(self.id, []):
            return True

        return False

    def __repr__(self):
        return '<Organization %s>' % self.name

    def __str__(self):
        return self.name

reversion.register(Organization, follow=['namespace_ptr'])


@reversion.register
class Project(models.Model):

    STATUS = Choices('active', 'deleted')
    status = StatusField()

    name = models.CharField('name', max_length=32,
                            validators=[
                                validators.RegexValidator(EXTENDED_NAME_REGEX, 'Enter a valid project name.', 'invalid')
                            ])
    namespace = models.ForeignKey(Namespace, related_name='projects')
    description = models.TextField('description')

    objects = UserFilteringQuerySet.as_manager()

    @classmethod
    def is_visible_q(cls, prefix, user):
        return Namespace.is_visible_q(prefix + 'namespace__', user) & (
            (prefix_q(prefix, status='active')) |
            (cls.is_visible_despite(prefix, user))
        )

    @staticmethod
    def is_visible_despite(prefix, user):
        if not user.is_authenticated():
            return Q()
        return (
            (prefix_q(prefix, teams__users=user)) |
            (prefix_q(prefix, namespace__repouser=user)) |
            ((prefix_q(prefix, namespace__organization__teams__is_all_projects=True) | prefix_q(prefix, namespace__organization__teams__projects__id=F('id'))) & prefix_q(prefix, namespace__organization__teams__users=user))
        )


    def full_name(self):
        return "{}/{}".format(self.namespace.name, self.name)

    def user_has_permission(self, user, perm_slug):
        if isinstance(user, AnonymousUser):
            return False

        ownerships = user.__dict__.setdefault('_project_ownerships', dict())
        permissions = user.__dict__.setdefault('_project_permissions', dict())
        if ownerships.get(self.id) is None:
            qs = self.teams.filter(users=user)
            if qs.filter(is_owner_team=True).count():
                ownerships[self.id] = True
            else:
                permissions[self.id] = qs.values_list('permissions__slug', flat=True)

        if self.id in ownerships:
            return True
        if perm_slug in permissions.get(self.id, []):
            return True

        return Namespace.objects.get_subclass(id=self.namespace_id).user_has_permission(user, perm_slug, project=self)

    def __repr__(self):
        return '<Project %s by %s>' % (self.name, self.namespace.name)

    def __str__(self):
        return self.name

    class Meta:
        unique_together = ('namespace', 'name')


@reversion.register
class Version(models.Model):

    STATUS = Choices('active', 'deleted')
    status = StatusField()

    name = models.CharField('name', max_length=32,
                            validators=[
                                validators.RegexValidator(TRIM_NAME_REGEX, 'Enter a valid version name.', 'invalid')
                            ])
    description = models.TextField('description')
    project = models.ForeignKey(Project, related_name='versions')

    objects = UserFilteringQuerySet.as_manager()

    @classmethod
    def is_visible_q(cls, prefix, user):
        return Project.is_visible_q(prefix + 'project__', user) & (
            (prefix_q(prefix, status='active')) |
            (cls.is_visible_despite(prefix, user))
        )

    @staticmethod
    def is_visible_despite(prefix, user):
        return Project.is_visible_despite(prefix + 'project__', user)

    def __repr__(self):
        return '<Version %s of %s>' % (self.name, self.project.name)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        from django.core.urlresolvers import reverse
        return reverse('repo-versions-detail', kwargs={'namespace': self.project.namespace.name, 'project': self.project.name, 'version': self.name})

    def full_name(self):
        return "{}/{}".format(self.project.full_name(), self.name)

    class Meta:
        ordering = ['-pk']
        unique_together = ('project', 'name')


def file_upload(instance, filename):
    import posixpath
    import uuid
    uuid_bit = uuid.uuid4().hex
    return posixpath.join('files', uuid_bit, filename)

@reversion.register
class File(models.Model):

    STATUS = Choices('active', 'deleted')
    status = StatusField()

    name = models.CharField('name', max_length=32,
                            validators=[
                                validators.RegexValidator(TRIM_NAME_REGEX, 'Enter a valid file name.', 'invalid')
                            ])
    description = models.TextField('description')
    version = models.ForeignKey(Version, related_name='files')

    file = models.FileField(upload_to=file_upload, blank=False, null=False)
    file_extension = models.CharField('extension', max_length=12, blank=False, null=False)
    file_size = models.PositiveIntegerField(null=True, blank=False)

    objects = UserFilteringQuerySet.as_manager()

    @classmethod
    def is_visible_q(cls, prefix, user):
        return Version.is_visible_q(prefix + 'version__', user) & (
            (prefix_q(prefix, status='active')) |
            (cls.is_visible_despite(prefix, user))
        )

    @staticmethod
    def is_visible_despite(prefix, user):
        return Version.is_visible_despite(prefix + 'version__', user)

    def full_name(self):
        return "{}/{}".format(self.version.full_name(), self.name)

    def __repr__(self):
        return '<File %s in %s of %s>' % (self.name, self.version.name, self.version.project.name)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['-pk']
        unique_together = ('version', 'name')


@reversion.register
class Permission(models.Model):
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=64, null=False, blank=False)
    description = models.TextField(null=False, blank=False)
    applies_to_project = models.BooleanField(default=True)

    def __repr__(self):
        props = ['applies_to_project'] if self.applies_to_project else []
        return '<Permission %s [%s]>' % (self.slug, ' '.join(props))

    def __str__(self):
        return self.slug


class Team(models.Model):
    name = models.CharField('name', max_length=80, null=False, blank=False)
    users = models.ManyToManyField(RepoUser, related_name='%(class)ss', blank=True)
    permissions = models.ManyToManyField(Permission, related_name='+', blank=True)
    is_owner_team = models.BooleanField(default=False)

    def __repr__(self):
        props = ['owner'] if self.is_owner_team else []
        return '<Team %s [%s]>' % (self.name, ' '.join(props))

    def __str__(self):
        return self.name

    def check_consistent(self):
        return True

    def make_consistent(self):
        return

    class Meta:
        abstract = True


@reversion.register
class OrganizationTeam(Team):
    organization = models.ForeignKey(Organization, related_name='teams')
    projects = models.ManyToManyField(Project, related_name='organizationteams', blank=True)
    is_all_projects = models.BooleanField(default=False)

    def make_consistent(self):
        self.projects = self.projects.filter(namespace=self.organization)
        self.save()

    def check_consistent(self):
        return self.projects.exclude(namespace=self.organization).count() == 0

    def __str__(self):
        return self.name

    def __repr__(self):
        props = (['all_projects'] if self.is_all_projects else []) + (['owner'] if self.is_owner_team else [])
        return '<OrganizationTeam %s in %s [%s]>' % (self.name, self.organization.name, ' '.join(props))

    class Meta:
        unique_together = ('organization', 'name')


@reversion.register
class ProjectTeam(Team):
    project = models.ForeignKey(Project, related_name='teams')

    def __str__(self):
        return self.name

    def __repr__(self):
        props = ['owner'] if self.is_owner_team else []
        return '<ProjectTeam %s in %s [%s]>' % (self.name, self.project.name, ' '.join(props))

    # TODO: we need to check here that if we're a user's project and we're the owner team, that that user is in us!

    class Meta:
        unique_together = ('project', 'name')


@reversion.register
class Flag(models.Model):
    STATUS = Choices('new', 'quashed', 'retracted', 'content_removed_moderator', 'content_removed_creator')
    status = StatusField()

    flagger = models.ForeignKey(RepoUser, null=False, blank=False, related_name='flagger_flags')
    resolver = models.ForeignKey(RepoUser, null=False, blank=False, related_name='resolver_flags')
    date_flagged = models.DateTimeField(auto_now_add=True, null=False, blank=False)
    date_resolved = models.DateTimeField(null=True, blank=True, default=None)

    FLAG_TYPE = Choices('inappropriate', 'spam')
    flag_type = StatusField(choices_name='FLAG_TYPE')

    extra_comments = models.TextField(blank=True, null=False)

    content_type = models.ForeignKey(ContentType, null=False, blank=False)
    object_id = models.PositiveIntegerField(null=False, blank=False)
    content_object = GenericForeignKey('content_type', 'object_id')

    class Meta:
        unique_together = ('flagger', 'flag_type', 'content_type', 'object_id')

    @classmethod
    def create_flag(cls, flag_content, flag_type, flagger):
        return Flag.objects.get_or_create(content_object=flag_content, flag_type=flag_type, flagger=flagger)

    def remove_content(self, user):
        if self.status != self.STATUS.new:
            raise ValueError("Incorrect state")

        self.status = self.STATUS.content_removed_creator if not user.is_staff else self.STATUS.content_removed_moderator
        self.content_object.status = self.content_object.STATUS.deleted
        self.date_resolved = timezone.now()
        self.resolver = user
        self.save()

    def quash(self, user):
        if self.status != self.STATUS.new:
            raise ValueError("Incorrect state")

        self.status = self.STATUS.quashed
        self.date_resolved = timezone.now()
        self.resolver = user
        self.save()

    def retract(self, user):
        if self.status != self.STATUS.new:
            raise ValueError("Incorrect state")

        self.status = self.STATUS.retracted
        self.date_resolved = timezone.now()
        self.resolver = user
        self.save()


def create_project_owner_team(sender, instance, created, **kwargs):
    if instance and created:
        owning_namespace = Namespace.objects.get_subclass(id=instance.namespace_id)
        if isinstance(owning_namespace, RepoUser):
            team = ProjectTeam.objects.create(
                    project=instance,
                    is_owner_team=True,
                    name='Owners',
            )
            team.users = [owning_namespace]
            team.save()
post_save.connect(create_project_owner_team, sender=Project)

def create_organization_owner_team(sender, instance, created, **kwargs):
    if instance and created:
        OrganizationTeam.objects.create(
                name='Owners',
                organization=instance,
                is_all_projects=True,
                is_owner_team=True,
        )
post_save.connect(create_organization_owner_team, sender=Organization)
