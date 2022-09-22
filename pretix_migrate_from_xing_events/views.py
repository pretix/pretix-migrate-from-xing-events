import logging
from urllib.parse import quote, urljoin

import requests
from celery.result import AsyncResult
from dateutil.parser import parse
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from pretix.base.forms import SettingsForm, SecretKeySettingsField, SECRET_REDACTED
from pretix.control.permissions import OrganizerPermissionRequiredMixin
from pretix.control.views.organizer import OrganizerSettingsFormView
from pretix_migrate_from_xing_events.importer.client import XINGEventsAPIClient
from .tasks import import_from_xing

logger = logging.getLogger(__name__)


class ApiSettingsForm(SettingsForm):
    pretix_migrate_from_xing_events_email = forms.EmailField(
        label=_('E-mail address you use to log into XING Events'),
        required=False
    )
    pretix_migrate_from_xing_events_apikey = SecretKeySettingsField(
        label=_('XING Events API Key'),
        help_text=_(
            'To create the API Key, go to "My Account", then "API Key", then "Create API Key" in your XING Events '
            'dashboard. The name of the key does not matter, the API key type can be "Watcher keys", as we do not '
            'need write access.'),
        required=False,
    )

    def clean(self):
        data = super().clean()

        key = data['pretix_migrate_from_xing_events_apikey']
        if key == SECRET_REDACTED:
            key = self.obj.settings.pretix_migrate_from_xing_events_apikey

        if key:
            try:
                r = requests.get(
                    urljoin(XINGEventsAPIClient.base_url, 'user/find?username=' + quote(
                        data['pretix_migrate_from_xing_events_email']
                    )),
                    headers={
                        'Authorization': f'ApiKey {key}'
                    },
                    timeout=10,
                )
                if r.status_code == 403:
                    logger.error(f'XING events returned: {r.text}')
                    raise ValidationError(
                        _('XING Events returned an error that looks like your API key is invalid.')
                    )
                r.raise_for_status()
                d = r.json()
                if not d['success']:
                    logger.error(f'XING events returned: {r.text}')
                    raise ValidationError(
                        _('We were unable to reach XING Events to validate your key. Error message: {msg}').format(
                            msg={str(d["errors"])}
                        )
                    )

                if not d['ids']:
                    raise ValidationError(
                        _('We could not find a XING Events account matching your e-mail address based and '
                          'your API key. Please check again that the e-mail address is the correct one.')
                    )
                if len(d['ids']) > 1:
                    raise ValidationError(
                        _('We could find multiple XING Events account matching your e-mail address based and '
                          'your API key. Please contact pretix support.')
                    )

            except requests.RequestException as e:
                logger.exception('Could not reach XING events')
                raise ValidationError(
                    _('We were unable to reach XING Events to validate your key. Error message: {err}').format(
                        err=str(e)
                    )
                )

        return data


class IndexView(OrganizerSettingsFormView):
    template_name = "pretix_migrate_from_xing_events/index.html"
    permission = 'can_change_organizer_settings'
    form_class = ApiSettingsForm

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            form.save()
            if form.has_changed():
                self.request.organizer.log_action(
                    'pretix.organizer.settings', user=self.request.user, data={
                        k: form.cleaned_data.get(k)
                        for k in form.changed_data
                    }
                )

            if form.cleaned_data["pretix_migrate_from_xing_events_apikey"]:
                return redirect(reverse(
                    'plugins:pretix_migrate_from_xing_events:selection',
                    kwargs={'organizer': self.request.organizer.slug}
                ))
            else:
                messages.success(self.request, _('Your changes have been saved.'))
                return redirect(reverse(
                    'plugins:pretix_migrate_from_xing_events:index',
                    kwargs={'organizer': self.request.organizer.slug}
                ))
        else:
            messages.error(self.request, _('We could not save your changes. See below for details.'))
            return self.get(request)


class SelectionView(OrganizerPermissionRequiredMixin, TemplateView):
    template_name = "pretix_migrate_from_xing_events/selection.html"
    permission = 'can_change_organizer_settings'

    def dispatch(self, request, *args, **kwargs):
        if self.events is None:
            return redirect(reverse(
                'plugins:pretix_migrate_from_xing_events:index',
                kwargs={'organizer': self.request.organizer.slug}
            ))
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        events = request.POST.getlist("event")
        with_vouchers = request.POST.get("import-codes") == "on"
        with_orders = request.POST.get("import-orders") == "on"

        res = import_from_xing.apply_async(
            kwargs={
                'organizer': request.organizer.pk,
                'events': events,
                'with_vouchers': with_vouchers,
                'with_orders': with_orders,
                'user': request.user.pk,
            }
        )
        kwargs = {
            'organizer': self.request.organizer.slug,
            'taskid': res.id
        }
        return redirect(reverse('plugins:pretix_migrate_from_xing_events:status', kwargs=kwargs))

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            events=self.events,
            **kwargs
        )

    @cached_property
    def events(self):
        try:
            c = XINGEventsAPIClient(self.request.organizer.settings.pretix_migrate_from_xing_events_apikey)
            d = c._get('user/find?username=' + quote(
                self.request.organizer.settings.pretix_migrate_from_xing_events_email
            ), timeout=10)
            if not d['ids']:
                messages.error(
                    self.request,
                    _('We could not find a XING Events account matching your e-mail address based and '
                      'your API key. Please check again that the e-mail address is the correct one.')
                )
                return None
            uid = d['ids'][0]

            page = 0
            events = []
            while True:
                d = c._get(f'user/{uid}/events?resultType=full&page={page}', timeout=10)
                events += d['events']
                if page >= d['lastPage']:
                    break
                else:
                    page += 1

            for e in events:
                e['selectedDate'] = parse(e['selectedDate'])
            return events
        except IOError as e:
            logger.exception('Could not reach XING events')
            messages.error(
                self.request,
                _(f'We were unable to reach XING Events to fetch your events. Error message: {msg}').format(
                    msg=str(e)
                )
            )
            return None

class StatusView(OrganizerPermissionRequiredMixin, TemplateView):
    template_name = "pretix_migrate_from_xing_events/status.html"
    permission = 'can_change_organizer_settings'

    def get_context_data(self, **kwargs):
        r = AsyncResult(kwargs['taskid'])
        events = []
        if r.successful():
            events = self.request.organizer.events.filter(slug__in=r.result)
        return super().get_context_data(
            result=r,
            events=events,
            **kwargs
        )
