import hashlib
import json
import logging

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.functional import cached_property

from dirtyfields import DirtyFieldsMixin
from rest_framework.reverse import reverse
from reversion import revisions as reversion

from normandy.base.api.renderers import CanonicalJSONRenderer
from normandy.base.utils import filter_m2m, get_client_ip
from normandy.recipes.decorators import approved_revision_property
from normandy.recipes.geolocation import get_country_code
from normandy.recipes.utils import Autographer
from normandy.recipes.validators import validate_json


logger = logging.getLogger(__name__)


class Channel(models.Model):
    slug = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        ordering = ('slug',)

    def __repr__(self):
        return '<Channel {}>'.format(self.slug)


class Country(models.Model):
    code = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        ordering = ('name',)

    def __repr__(self):
        return '<Country {}>'.format(self.code)


class Locale(models.Model):
    code = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        ordering = ('name',)

    def __repr__(self):
        return '<Locale {}>'.format(self.code)


class Signature(models.Model):
    signature = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)
    public_key = models.TextField()
    x5u = models.TextField(null=True)


class RecipeQuerySet(models.QuerySet):
    def update_signatures(self):
        """
        Update the signatures on all Recipes in the queryset.
        """
        autographer = Autographer()
        # Convert to a list because order must be preserved
        recipes = list(self)

        recipe_ids = [r.id for r in recipes]
        logger.info(
            'Requesting signatures for recipes with ids [%s] from Autograph',
            (recipe_ids, ),
            extra={'recipe_ids': recipe_ids})

        canonical_jsons = [r.canonical_json() for r in recipes]
        signatures_data = autographer.sign_data(canonical_jsons)

        for recipe, sig_data in zip(recipes, signatures_data):
            signature = Signature(**sig_data)
            signature.save()
            recipe.signature = signature
            recipe.save()


class Recipe(DirtyFieldsMixin, models.Model):
    """A set of actions to be fetched and executed by users."""
    objects = RecipeQuerySet.as_manager()

    latest_revision = models.ForeignKey('RecipeRevision', null=True, on_delete=models.SET_NULL,
                                        related_name='latest_for_recipe')
    approved_revision = models.ForeignKey('RecipeRevision', null=True, on_delete=models.SET_NULL,
                                          related_name='approved_for_recipe')

    enabled = models.BooleanField(default=False)
    signature = models.OneToOneField(Signature, related_name='recipe_revision', null=True,
                                     blank=True)

    class Meta:
        ordering = ['-enabled', '-latest_revision__updated']

    def __repr__(self):
        return '<Recipe "{name}">'.format(name=self.name)

    def __str__(self):
        return self.name

    @property
    def is_approved(self):
        return self.approved_revision is not None

    @approved_revision_property
    def name(self, revision):
        return revision.name

    @approved_revision_property
    def action(self, revision):
        return revision.action

    @approved_revision_property
    def extra_filter_expression(self, revision):
        return revision.extra_filter_expression

    @approved_revision_property
    def arguments_json(self, revision):
        return revision.arguments_json

    @approved_revision_property
    def arguments(self, revision):
        return revision.arguments

    @approved_revision_property
    def revision_id(self, revision):
        return revision.id

    @approved_revision_property
    def last_updated(self, revision):
        return revision.updated

    @approved_revision_property
    def filter_expression(self, revision):
        return revision.filter_expression

    @approved_revision_property
    def channels(self, revision):
        return revision.channels

    @approved_revision_property
    def countries(self, revision):
        return revision.countries

    @approved_revision_property
    def locales(self, revision):
        return revision.locales

    def canonical_json(self):
        from normandy.recipes.api.serializers import RecipeSerializer  # Avoid circular import
        data = RecipeSerializer(self).data
        return CanonicalJSONRenderer().render(data)

    def update_signature(self):
        autographer = Autographer()

        logger.info(
            'Requesting signatures for recipes with ids [%s] from Autograph',
            self.id,
            extra={'recipe_ids': [self.id]})

        signature_data = autographer.sign_data([self.canonical_json()])[0]
        signature = Signature(**signature_data)
        signature.save()
        self.signature = signature

    @transaction.atomic
    def update(self, force=False, **data):
        revision = self.latest_revision

        if revision:
            revisions = RecipeRevision.objects.filter(id=revision.id)

            revision_data = revision.data
            revision_data.update(data)

            channels = revision_data.pop('channels')
            revisions = filter_m2m(revisions, 'channels', channels)

            countries = revision_data.pop('countries')
            revisions = filter_m2m(revisions, 'countries', countries)

            locales = revision_data.pop('locales')
            revisions = filter_m2m(revisions, 'locales', locales)

            data = revision_data
            revisions = revisions.filter(**data)

            is_clean = revisions.exists()
        else:
            channels = data.pop('channels', [])
            countries = data.pop('countries', [])
            locales = data.pop('locales', [])
            is_clean = False

        if not is_clean or force:
            if revision and revision.approval_status == RecipeRevision.PENDING:
                revision.approval_request.delete()

            self.latest_revision = RecipeRevision.objects.create(
                recipe=self, parent=revision, **data)

            for channel in channels:
                self.latest_revision.channels.add(channel)

            for country in countries:
                self.latest_revision.countries.add(country)

            for locale in locales:
                self.latest_revision.locales.add(locale)

            self.save()

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.is_dirty(check_relationship=True):
            dirty_fields = self.get_dirty_fields(check_relationship=True)
            dirty_field_names = list(dirty_fields.keys())

            if (len(dirty_field_names) > 1 and 'signature' in dirty_field_names
                    and self.signature is not None):
                # Setting the signature while also changing something else is probably
                # going to make the signature immediately invalid. Don't allow it.
                raise ValidationError('Signatures must change alone')

            if dirty_field_names != ['signature']:
                super().save(*args, **kwargs)
                kwargs['force_insert'] = False

                try:
                    self.update_signature()
                except ImproperlyConfigured:
                    self.signature = None

        super().save(*args, **kwargs)


class RecipeRevision(models.Model):
    APPROVED = 'approved'
    REJECTED = 'rejected'
    PENDING = 'pending'

    id = models.CharField(max_length=64, primary_key=True)
    parent = models.OneToOneField('self', null=True, on_delete=models.CASCADE,
                                  related_name='child')
    recipe = models.ForeignKey(Recipe, related_name='revisions')
    created = models.DateTimeField(default=timezone.now)
    updated = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='recipe_revisions',
                             null=True)
    comment = models.TextField()

    name = models.CharField(max_length=255)
    action = models.ForeignKey('Action', related_name='recipe_revisions')
    arguments_json = models.TextField(default='{}', validators=[validate_json])
    extra_filter_expression = models.TextField(blank=False)
    channels = models.ManyToManyField(Channel)
    countries = models.ManyToManyField(Country)
    locales = models.ManyToManyField(Locale)

    @property
    def data(self):
        return {
            'name': self.name,
            'action': self.action,
            'arguments_json': self.arguments_json,
            'extra_filter_expression': self.extra_filter_expression,
            'channels': list(self.channels.all()),
            'countries': list(self.countries.all()),
            'locales': list(self.locales.all()),
        }

    @property
    def filter_expression(self):
        parts = []

        if self.locales.count():
            locales = ', '.join(["'{}'".format(l.code) for l in self.locales.all()])
            parts.append('normandy.locale in [{}]'.format(locales))

        if self.countries.count():
            countries = ', '.join(["'{}'".format(c.code) for c in self.countries.all()])
            parts.append('normandy.country in [{}]'.format(countries))

        if self.channels.count():
            channels = ', '.join(["'{}'".format(c.slug) for c in self.channels.all()])
            parts.append('normandy.channel in [{}]'.format(channels))

        if self.extra_filter_expression:
            parts.append(self.extra_filter_expression)

        expression = ') && ('.join(parts)

        return '({})'.format(expression) if len(parts) > 1 else expression

    @property
    def arguments(self):
        return json.loads(self.arguments_json)

    @arguments.setter
    def arguments(self, value):
        self.arguments_json = json.dumps(value)

    @property
    def serializable_recipe(self):
        """Returns an unsaved recipe object with this revisions data to be serialized."""
        recipe = self.recipe
        recipe.approved_revision = self if self.approval_status == self.APPROVED else None
        recipe.latest_revision = self
        return recipe

    @property
    def approval_status(self):
        try:
            if self.approval_request.approved is True:
                return self.APPROVED
            elif self.approval_request.approved is False:
                return self.REJECTED
            else:
                return self.PENDING
        except ApprovalRequest.DoesNotExist:
            return None

    def hash(self):
        data = '{}{}{}{}{}{}'.format(self.recipe.id, self.created, self.name, self.action.id,
                                     self.arguments_json, self.filter_expression)
        return hashlib.sha256(data.encode()).hexdigest()

    def save(self, *args, **kwargs):
        if not self.created:
            self.created = timezone.now()
        self.id = self.hash()
        self.updated = timezone.now()
        super().save(*args, **kwargs)


class ApprovalRequest(models.Model):
    revision = models.OneToOneField(RecipeRevision, related_name='approval_request')
    created = models.DateTimeField(default=timezone.now)
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='approval_requests',
                                null=True)
    approved = models.NullBooleanField(null=True)
    approver = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='approved_requests',
                                 null=True)

    class AlreadyApproved(Exception):
        pass

    def approve(self, approver):
        if self.approved is not None:
            raise self.AlreadyApproved()

        self.approved = True
        self.approver = approver
        self.save()

        recipe = self.revision.recipe
        recipe.approved_revision = self.revision
        recipe.save()

    def reject(self, approver):
        if self.approved is not None:
            raise self.AlreadyApproved()

        self.approved = False
        self.approver = approver
        self.save()


@reversion.register()
class Action(models.Model):
    """A single executable action that can take arguments."""
    name = models.SlugField(max_length=255, unique=True)

    implementation = models.TextField()
    implementation_hash = models.CharField(max_length=40, editable=False)

    arguments_schema_json = models.TextField(default='{}', validators=[validate_json])

    @property
    def arguments_schema(self):
        return json.loads(self.arguments_schema_json)

    @arguments_schema.setter
    def arguments_schema(self, value):
        self.arguments_schema_json = json.dumps(value)

    @property
    def recipes_used_by(self):
        """Set of enabled recipes that are using this action."""
        return Recipe.objects.filter(
            latest_revision_id__in=self.recipe_revisions.values_list('id', flat=True),
            enabled=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('action-detail', args=[self.name])

    def compute_implementation_hash(self):
        return hashlib.sha1(self.implementation.encode()).hexdigest()

    def save(self, *args, **kwargs):
        # Save first so the upload is available.
        super().save(*args, **kwargs)

        # Update hash
        self.implementation_hash = self.compute_implementation_hash()
        super().save(update_fields=['implementation_hash'])


class Client(object):
    """A client attempting to fetch a set of recipes."""

    def __init__(self, request=None, **kwargs):
        self.request = request
        for key, value in kwargs.items():
            setattr(self, key, value)

    @cached_property
    def country(self):
        ip_address = get_client_ip(self.request)
        if ip_address is None:
            return None
        else:
            return get_country_code(ip_address)

    @cached_property
    def request_time(self):
        return self.request.received_at
