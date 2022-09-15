import bleach
import pytz
import requests
from dateutil.parser import parse
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.crypto import get_random_string
from i18nfield.strings import LazyI18nString

from pretix.base.models import Event
from pretix.base.templatetags.rich_text import ALLOWED_TAGS, ALLOWED_ATTRIBUTES, ALLOWED_PROTOCOLS
from .client import XINGEventsAPIClient


class XINGEventsImporter:

    def __init__(self, apikey, organizer):
        self.client = XINGEventsAPIClient(apikey=apikey)
        self.organizer = organizer

    @transaction.atomic()
    def import_event(self, event_id):
        self._import_event_data(event_id)

    def _clean_html(self, data):
        return bleach.clean(
            data,
            strip=True,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=ALLOWED_PROTOCOLS,
        )

    def _clone_file(self, event, url, basename):
        r = requests.get(url)
        r.raise_for_status()
        value = ContentFile(r.content)
        nonce = get_random_string(length=8)
        fname = 'pub/%s/%s/%s.%s.%s' % (
            event.organizer.slug, event.slug, basename, nonce, url.rsplit('.', 1)[-1]
        )
        newname = default_storage.save(fname, value)
        value.name = newname
        return value

    def _import_event_data(self, event_id):
        d = self.client._get(f'event/{event_id}')['event']

        try:
            event = self.organizer.events.get(slug=d['identifier'])
        except Event.DoesNotExist:
            event = Event(slug=d['identifier'], organizer=self.organizer)

        language = d['language'] or 'de'
        tz = pytz.timezone(d['timezone'] or 'Europe/Berlin')

        event.name = LazyI18nString({language: d['title']})
        event.date_from = tz.localize(parse(d['selectedDate']))
        event.date_to = tz.localize(parse(d['selectedEndDate'])) if d['selectedEndDate'] else None
        if d['longitude']:
            event.geo_lon = d['longitude']
        if d['latitude']:
            event.geo_lat = d['latitude']

        lang = [
            d.get('location'),
            d.get('street'),
            d.get('street2'),
            f"{d.get('zipCode') or ''} {d.get('city') or ''}",
            d.get('locationDescription'),
        ]
        event.location = LazyI18nString({language: '\n'.join([l for l in lang if l and l.strip()])})

        event.save()

        # multi-lang not supported?
        event.settings.locales = [language]
        event.settings.locale = language
        event.settings.region = d['country'] or 'DE'
        event.settings.timezone = d['timezone'] or 'Europe/Berlin'
        event.settings.meta_noindex = not d['publishSearchEngines']
        event.settings.show_times = not d['hideTime']
        if d['organizerEmail']:
            event.settings.contact_mail = d['organizerEmail']
        if d['description']:
            event.settings.frontpage_text = LazyI18nString({language: self._clean_html(d['description'])})

        if d['internalReference']:
            prop_ref = self.organizer.meta_properties.get_or_create(name="Interne Referenz")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['internalReference']))

        if d['onlineType']:
            prop_ref = self.organizer.meta_properties.get_or_create(name="Online-Typ")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['onlineType']))

        if d['accessibility']:
            prop_ref = self.organizer.meta_properties.get_or_create(name="Barrierefreiheit")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['accessibility']))

        if d['type']:
            prop_ref = self.organizer.meta_properties.get_or_create(name="Typ")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['type'].replace('EVENT_TYPE_', '')))

        if d['twitterHashtag']:
            prop_ref = self.organizer.meta_properties.get_or_create(name="Twitter-Hashtag")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['twitterHashtag']))

        if d['banner']:
            event.settings.logo_image = self._clone_file(event, d['banner'], 'logo_image')
            event.settings.logo_image_large = True
        elif d['logo']:
            event.settings.logo_image = self._clone_file(event, d['logo'], 'logo_image')
            event.settings.logo_image_large = True

        # todo: onlineUrl → digitalcontent?
        # todo: schedule → pages?
