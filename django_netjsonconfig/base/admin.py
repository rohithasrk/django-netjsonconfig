import logging

from django import forms
from django.conf import settings
from django.conf.urls import url
from django.contrib import admin
from django.contrib.admin.templatetags.admin_static import static
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _

from .. import settings as app_settings
from ..utils import send_file
from ..widgets import JsonSchemaWidget

logger = logging.getLogger(__name__)
prefix = 'django-netjsonconfig/'

if 'reversion' in settings.INSTALLED_APPS:
    from reversion.admin import VersionAdmin as ModelAdmin
else:  # pragma: nocover
    from django.contrib.admin import ModelAdmin


class TimeReadonlyMixin(object):
    """
    mixin that automatically flags
    `created` and `modified` as readonly
    """
    def __init__(self, *args, **kwargs):
        self.readonly_fields += ('created', 'modified',)
        super(TimeReadonlyMixin, self).__init__(*args, **kwargs)


class BaseAdmin(TimeReadonlyMixin, ModelAdmin):
    pass


# TODO: kept for backward compatibility, remove in 0.7.0
TimeStampedEditableAdmin = BaseAdmin


class BaseConfigAdmin(BaseAdmin):
    preview_template = None
    actions_on_bottom = True
    save_on_top = True

    class Media:
        css = {'all': (static('{0}css/admin.css'.format(prefix)),)}
        js = [static('{0}js/{1}'.format(prefix, f))
              for f in ('preview.js',
                        'unsaved_changes.js',
                        'uuid.js',
                        'switcher.js')]

    def get_extra_context(self, pk=None):
        prefix = 'admin:{0}_{1}'.format(self.opts.app_label, self.model.__name__.lower())
        ctx = {'preview_url': reverse('{0}_preview'.format(prefix))}
        if pk:
            ctx.update({'download_url': reverse('{0}_download'.format(prefix), args=[pk])})
        return ctx

    def add_view(self, request, form_url='', extra_context=None):
        extra_context = self.get_extra_context()
        instance = self.model()
        if hasattr(instance, 'get_default_templates'):
            templates = instance.get_default_templates()
            templates = [str(t.id) for t in templates]
            extra_context.update({'default_templates': templates})
        return super(BaseConfigAdmin, self).add_view(request, form_url, extra_context)

    def change_view(self, request, pk, form_url='', extra_context=None):
        extra_context = self.get_extra_context(pk)
        return super(BaseConfigAdmin, self).change_view(request, pk, form_url, extra_context)

    def get_urls(self):
        options = getattr(self.model, '_meta')
        url_prefix = '{0}_{1}'.format(options.app_label, options.model_name)
        return [
            url(r'^download/(?P<pk>[^/]+)/$',
                self.admin_site.admin_view(self.download_view),
                name='{0}_download'.format(url_prefix)),
            url(r'^preview/$',
                self.admin_site.admin_view(self.preview_view),
                name='{0}_preview'.format(url_prefix))
        ] + super(BaseConfigAdmin, self).get_urls()

    def _get_config_model(self):
        model = self.model
        if hasattr(model, 'get_backend_instance'):
            return model
        return model.get_config_model()

    def _get_preview_instance(self, request):
        """
        returns a temporary preview instance used for preview
        """
        kwargs = {}
        config_model = self._get_config_model()
        for key, value in request.POST.items():
            # skip keys that are not model fields
            try:
                field = config_model._meta.get_field(key)
            except FieldDoesNotExist:
                continue
            # skip m2m
            if field.many_to_many:
                continue
            # skip if falsy value and PK or relations
            elif not value and any([field.primary_key, field.is_relation]):
                continue
            # adapt attribute names to the fact that we only
            # have pk of relations, therefore use {relation}_id
            elif field.is_relation:
                key = '{relation}_id'.format(relation=key)
                # pass non-empty string or None
                kwargs[key] = value or None
            # put regular field values in kwargs dict
            else:
                kwargs[key] = value
        # this object is instanciated only to generate the preview
        # it won't be saved to the database
        instance = config_model(**kwargs)
        instance.full_clean(exclude=['device'],
                            validate_unique=False)
        return instance

    preview_error_msg = _('Preview for {0} with name {1} failed')

    def preview_view(self, request):
        if request.method != 'POST':
            msg = _('Preview: request method {0} is not allowed').format(request.method)
            logger.warning(msg, extra={'request': request, 'stack': True})
            return HttpResponse(status=405)
        config_model = self._get_config_model()
        error = None
        output = None
        # error message for eventual exceptions
        error_msg = self.preview_error_msg.format(config_model.__name__, request.POST.get('name'))
        try:
            instance = self._get_preview_instance(request)
        except Exception as e:
            logger.exception(error_msg, extra={'request': request})
            # return 400 for validation errors, otherwise 500
            status = 400 if e.__class__ is ValidationError else 500
            return HttpResponse(str(e), status=status)
        template_ids = request.POST.get('templates')
        if template_ids:
            template_model = config_model.get_template_model()
            templates = template_model.objects.filter(pk__in=template_ids.split(','))
            try:
                templates = list(templates)  # evaluating queryset performs query
            except ValueError as e:
                logger.exception(error_msg, extra={'request': request})
                return HttpResponse(str(e), status=400)
        else:
            templates = None
        if not error:
            backend = instance.get_backend_instance(template_instances=templates)
            try:
                instance.clean_netjsonconfig_backend(backend)
                output = backend.render()
            except ValidationError as e:
                error = str(e)
        context = self.admin_site.each_context(request)
        opts = self.model._meta
        context.update({
            'is_popup': True,
            'opts': opts,
            'change': False,
            'output': output,
            'media': self.media,
            'error': error,
        })
        return TemplateResponse(request, self.preview_template or [
            'admin/%s/%s/preview.html' % (opts.app_label, opts.model_name),
            'admin/%s/preview.html' % opts.app_label
        ], context)

    def download_view(self, request, pk):
        instance = get_object_or_404(self.model, pk=pk)
        if hasattr(instance, 'generate'):
            config = instance
        elif hasattr(instance, 'config'):
            config = instance.config
        else:
            raise Http404()
        config_archive = config.generate()
        return send_file(filename='{0}.tar.gz'.format(config.name),
                         contents=config_archive.getvalue())


class BaseForm(forms.ModelForm):
    """
    Adds support for ``NETJSONCONFIG_DEFAULT_BACKEND``
    """
    if app_settings.DEFAULT_BACKEND:
        def __init__(self, *args, **kwargs):
            if 'initial' not in kwargs:
                kwargs['initial'] = {}
            kwargs['initial'].update({'backend': app_settings.DEFAULT_BACKEND})
            super(BaseForm, self).__init__(*args, **kwargs)

    class Meta:
        exclude = []
        widgets = {'config': JsonSchemaWidget}


class AbstractConfigForm(BaseForm):
    def clean_templates(self):
        config_model = self.Meta.model
        # copy cleaned_data to avoid tampering with it
        data = self.cleaned_data.copy()
        templates = data.pop('templates', [])
        if self.instance._state.adding:
            # when adding self.instance is empty, we need to create a
            # temporary instance that we'll use just for validation
            config = config_model(**data)
        else:
            config = self.instance
        if config.backend and templates:
            config_model.clean_templates(action='pre_add',
                                         instance=config,
                                         sender=config.templates,
                                         reverse=False,
                                         model=config.templates.model,
                                         pk_set=templates)
        return templates


class AbstractConfigInline(TimeReadonlyMixin, admin.StackedInline):
    verbose_name_plural = _('Device configuration details')
    readonly_fields = ['status', 'last_ip']
    fields = ['backend',
              'status',
              'last_ip',
              'templates',
              'config',
              'created',
              'modified']


class AbstractDeviceAdmin(BaseConfigAdmin):
    list_display = ['name', 'backend', 'status',
                    'last_ip', 'created', 'modified']
    search_fields = ['id', 'name', 'mac_address', 'key', 'model', 'os', 'system']
    list_filter = ['config__backend',
                   'config__templates',
                   'config__status',
                   'created']
    list_select_related = ('config',)
    readonly_fields = ['id_hex']
    fields = ['name',
              'mac_address',
              'id_hex',
              'key',
              'model',
              'os',
              'system',
              'created',
              'modified']

    def id_hex(self, obj):
        return obj.pk.hex

    id_hex.short_description = "UUID"

    def _get_fields(self, fields, request, obj=None):
        """
        removes "id" field in add view
        """
        if obj:
            return fields
        new_fields = fields[:]
        if 'id_hex' in new_fields:
            new_fields.remove('id_hex')
        return new_fields

    def get_fields(self, request, obj=None):
        return self._get_fields(self.fields, request, obj)

    def get_readonly_fields(self, request, obj=None):
        return self._get_fields(self.readonly_fields, request, obj)

    def _get_preview_instance(self, request):
        c = super(AbstractDeviceAdmin, self)._get_preview_instance(request)
        c.device = self.model(id=request.POST.get('id'),
                              name=request.POST.get('name'),
                              mac_address=request.POST.get('mac_address'),
                              key=request.POST.get('key'))
        return c


class AbstractTemplateAdmin(BaseConfigAdmin):
    list_display = ['name', 'type', 'backend', 'default', 'created', 'modified']
    list_filter = ['backend', 'type', 'default', 'created']
    search_fields = ['name']
    fields = ['name',
              'type',
              'backend',
              'vpn',
              'auto_cert',
              'tags',
              'default',
              'config',
              'created',
              'modified']


class AbstractVpnForm(forms.ModelForm):
    """
    Adds support for ``NETJSONCONFIG_DEFAULT_BACKEND``
    """
    if app_settings.DEFAULT_VPN_BACKEND:
        def __init__(self, *args, **kwargs):
            if 'initial' in kwargs:
                kwargs['initial'].update({'backend': app_settings.DEFAULT_VPN_BACKEND})
            super(AbstractVpnForm, self).__init__(*args, **kwargs)

    class Meta:
        widgets = {
            'config': JsonSchemaWidget,
            'dh': forms.widgets.HiddenInput
        }
        exclude = []


class AbstractVpnAdmin(BaseConfigAdmin):
    list_display = ['name', 'backend', 'created', 'modified']
    list_filter = ['backend', 'ca', 'created']
    search_fields = ['id', 'name', 'host']
    fields = ['name',
              'host',
              'ca',
              'cert',
              'backend',
              'notes',
              'dh',
              'config',
              'created',
              'modified']
