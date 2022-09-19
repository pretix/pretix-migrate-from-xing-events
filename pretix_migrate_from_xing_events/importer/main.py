import datetime
import json
import os
from decimal import Decimal
from itertools import chain
from urllib.parse import urljoin, urlparse

import bleach
import pytz
import requests
from dateutil.parser import parse
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.crypto import get_random_string
from django.utils.timezone import now
from i18nfield.strings import LazyI18nString

from pretix.base.channels import get_all_sales_channels
from pretix.base.models import Event, ItemMetaValue, Item, ItemVariation, Question, Order, OrderPayment, OrderPosition, \
    Checkin, QuestionAnswer, InvoiceAddress, OrderFee, Voucher, Quota
from pretix.base.settings import LazyI18nStringList
from pretix.base.templatetags.rich_text import ALLOWED_TAGS, ALLOWED_ATTRIBUTES, ALLOWED_PROTOCOLS
from .client import XINGEventsAPIClient


class XINGEventsImporter:

    def __init__(self, apikey, organizer):
        self.client = XINGEventsAPIClient(apikey=apikey)
        self.organizer = organizer
        self._tax_rule = None
        self.has_product_definitions = False

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

    def _money_conversion(self, currency, int_val):
        if currency in ('KRW', 'JPY'):
            return Decimal(int_val)
        else:
            return Decimal(int_val) / Decimal('100.00')

    def _import_event_data(self, event_id):
        d = self.client._get(f'event/{event_id}')['event']
        ts = self.client._get(f'event/{event_id}/ticketShop')['ticketShop']

        try:
            event = self.organizer.events.get(slug=d['identifier'])
        except Event.DoesNotExist:
            event = Event(slug=d['identifier'], organizer=self.organizer)

        language = d['language'] or 'de'
        tz = pytz.timezone(d['timezone'] or 'Europe/Berlin')

        event.name = LazyI18nString({language: d['title']})
        event.date_from = tz.localize(parse(d['selectedDate']))
        event.date_to = tz.localize(parse(d['selectedEndDate'])) if d.get('selectedEndDate') else None
        event.currency = ts['currency']

        event.presale_start = tz.localize(parse(ts['registrationStartDate'])) if ts.get('registrationStartDate') else None
        event.presale_end = tz.localize(parse(ts['registrationEndDate'])) if ts.get('registrationEndDate') else None

        if d.get('longitude'):
            event.geo_lon = d['longitude']
        if d.get('latitude'):
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

        if ts['commercial'] and ts['salesTax']:
            self._tax_rule = event.tax_rules.get_or_create(
                rate=Decimal(ts['salesTax']) / Decimal('100.00'),
                defaults={
                    'name': LazyI18nString({'de': 'MwSt', 'en': 'VAT'})
                }
            )[0]

        event.enable_plugin("pretix.plugins.badges")
        event.save()
        event.settings.name_scheme = "salutation_given_family"
        event.settings.attendee_names_asked = True
        event.settings.attendee_names_required = True
        event.settings.attendee_emails_asked = True
        event.settings.attendee_emails_required = True
        event.settings.locales = [language]
        event.settings.locale = language
        event.settings.region = d['country'] or 'DE'
        event.settings.timezone = d['timezone'] or 'Europe/Berlin'
        event.settings.meta_noindex = not d.get('publishSearchEngines')
        event.settings.show_times = not d.get('hideTime')
        event.settings.show_quota_left = bool(ts.get('showAvailableTickets'))

        if not ts.get('ticketsEditable') or not ts.get('ticketsTransferable'):
            event.settings.last_order_modification_date = tz.localize(datetime.datetime(1999, 1, 1, 0, 0, 0))

        if d.get('organizerEmail'):
            event.settings.contact_mail = d['organizerEmail']

        if d.get('description'):
            event.settings.frontpage_text = LazyI18nString({language: self._clean_html(d['description'])})

        if d.get('internalReference'):
            prop_ref = self.organizer.meta_properties.get_or_create(name="Interne Referenz")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['internalReference']))

        if d.get('onlineType'):
            prop_ref = self.organizer.meta_properties.get_or_create(name="Online-Typ")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['onlineType']))

        if d.get('accessibility'):
            prop_ref = self.organizer.meta_properties.get_or_create(name="Barrierefreiheit")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['accessibility']))

        if d.get('type'):
            prop_ref = self.organizer.meta_properties.get_or_create(name="Typ")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['type'].replace('EVENT_TYPE_', '')))

        if d.get('twitterHashtag'):
            prop_ref = self.organizer.meta_properties.get_or_create(name="Twitter-Hashtag")[0]
            event.meta_values.update_or_create(property=prop_ref, defaults=dict(value=d['twitterHashtag']))

        if d.get('banner'):
            event.settings.logo_image = self._clone_file(event, d['banner'], 'logo_image')
            event.settings.logo_image_large = True
        elif d.get('logo'):
            event.settings.logo_image = self._clone_file(event, d['logo'], 'logo_image')
            event.settings.logo_image_large = True

        if ts.get('vatId'):
            event.settings.invoice_address_from_vat_id = ts['vatId']

        confirmation_texts = LazyI18nStringList()
        if ts['ownTermsAndConditions']:
            url = ts['ownTermsAndConditions']
            if 'xing-events.com/' in ts['ownTermsAndConditions']:
                url = urljoin(
                    settings.SITE_URL,
                    urljoin(
                        settings.MEDIA_URL,
                        default_storage.url(self._clone_file(event, ts['ownTermsAndConditions'], 'terms').name)
                    )
                )
            confirmation_texts.append(LazyI18nString({
                'de': f'Ich akzeptiere die [AGB]({url}) von {d["organisatorDisplayName"]}',
                'en': f'I accept the [terms and conditions]({url}) of {d["organisatorDisplayName"]}',
            }))
        if ts['ownPrivacyPolicy']:
            url = ts['ownPrivacyPolicy']
            if 'xing-events.com/' in ts['ownPrivacyPolicy']:
                url = urljoin(
                    settings.SITE_URL,
                    urljoin(
                        settings.MEDIA_URL,
                        default_storage.url(self._clone_file(event, ts['ownPrivacyPolicy'], 'privacy').name)
                    )
                )

            confirmation_texts.append(LazyI18nString({
                'de': f'Ich habe die [Datenschutzerklärung]({url}) von {d["organisatorDisplayName"]} zur Kenntnis genommen',
                'en': f'I have read the [privacy policy]({url}) of {d["organisatorDisplayName"]}',
            }))
        if confirmation_texts:
            event.settings.confirm_texts = confirmation_texts

        # todo: onlineUrl → digitalcontent?
        # ticketShop.closed?

        admission_items = self._import_ticket_categories(event, language, event_id, ts['availableLimit'])
        self._import_product_definitions(event, language, event_id, admission_items)
        self._import_userdata_definitions(event, language, event_id, admission_items)
        self._import_code_definitions(event, language, event_id)

        # todo: remove after dev
        for order in event.orders.filter(testmode=True):
            order.gracefully_delete()

        self._import_payments(event, language, event_id)

    def _import_ticket_categories(self, event, language, event_id, global_quota_limit):
        prop_import_id = event.item_meta_properties.get_or_create(name="XING-Events-Ticketkategorie")[0]
        prop_comment = event.item_meta_properties.get_or_create(name="Kommentar")[0]
        item_category = event.categories.get_or_create(
            internal_name='Tickets', defaults={
                'name': LazyI18nString({'en': 'Tickets', 'de': 'Tickets'})
            }
        )[0]
        all_channels = list(get_all_sales_channels().keys())

        category_ids = self.client._get(f'event/{event_id}/ticketCategories')['ticketCategories']
        items = []
        for i, category_id in enumerate(category_ids):
            cat = self.client._get(f'ticketCategory/{category_id}')['ticketCategory']

            try:
                item = ItemMetaValue.objects.get(property=prop_import_id, value=str(category_id), item__event=event).item
                creating = False
            except ItemMetaValue.DoesNotExist:
                item = Item(event=event)
                creating = True

            item.name = LazyI18nString({language: cat['name']})
            item.admission = True
            item.category = item_category
            item.position = i

            if cat.get('ticketDescription'):
                item.description = LazyI18nString({language: self._clean_html(cat['ticketDescription'])})

            if cat.get('internalReference'):
                item.internal_name = cat['internalReference']

            if cat.get('price') is not None:
                item.default_price = self._money_conversion(event.currency, cat['price'])

            item.tax_rule = self._tax_rule
            item.sales_channels = all_channels
            item.available_from = event.timezone.localize(parse(cat['saleStart'])) if cat.get('saleStart') else None
            item.available_until = event.timezone.localize(parse(cat['saleEnd'])) if cat.get('saleEnd') else None
            item.min_per_order = cat.get('minSell') or 0
            item.max_per_order = cat.get('maxSell') or None
            item.active = cat['active']

            item.save()
            if creating:
                item.meta_values.create(property=prop_import_id, value=str(category_id))

            if cat.get('comment'):
                item.meta_values.update_or_create(property=prop_comment, defaults=dict(value=cat['comment']))

            quota = event.quotas.get_or_create(name=cat.get('internalReference') or cat['name'])[0]
            quota.size = cat['available'] + cat['sold']  # todo: also + cat['reservedCount ?
            quota.save()
            quota.items.add(item)

            items.append(item)

        total_quota = event.quotas.get_or_create(name="Gesamt-Teilnehmermenge")[0]
        total_quota.size = global_quota_limit
        total_quota.save()
        total_quota.items.add(*items)
        return items

    def _import_product_definitions(self, event, language, event_id, admission_items):
        prop_import_id = event.item_meta_properties.get_or_create(name="XING-Events-Produkt")[0]
        all_channels = list(get_all_sales_channels().keys())
        addon_category = None

        pd_ids = self.client._get(f'event/{event_id}/productDefinitions')['productDefinitions']
        addon_items = []
        for i, pd_id in enumerate(pd_ids):
            self.has_product_definitions = True
            pd = self.client._get(f'productDefinition/{pd_id}')['productDefinition']
            try:
                item = ItemMetaValue.objects.get(property=prop_import_id, value=str(pd_id),
                                                 item__event=event).item
                creating = False
            except ItemMetaValue.DoesNotExist:
                item = Item(event=event)
                creating = True

            if pd['type'] == 'PAYMENT':
                item_category = event.categories.get_or_create(
                    internal_name='Zusätze Bestellung', defaults={
                        'name': LazyI18nString({'en': 'Additional options', 'de': 'Zusätzliche Optionen'})
                    }
                )[0]
            else:
                item_category = addon_category = event.categories.get_or_create(
                    internal_name='Zusatzprodukte', is_addon=True, defaults={
                        'name': LazyI18nString({'en': 'Additional options', 'de': 'Zusätzliche Optionen'})
                    }
                )[0]
                addon_items.append(item)

            if len(pd['options']) > 1 or pd['options'][0]['productDefinitionOptionName'] == pd['title']:
                item.name = LazyI18nString({language: pd['title']})
            else:
                item.name = LazyI18nString(
                    {language: pd['title'] + ' ' + pd['options'][0]['productDefinitionOptionName']})
            item.admission = False
            item.category = item_category
            item.position = i
            item.tax_rule = self._tax_rule
            item.sales_channels = all_channels
            item.active = True
            item.max_per_order = 1
            item.default_price = Decimal('0.00')

            item.save()
            if creating:
                item.meta_values.create(property=prop_import_id, value=str(pd_id))

            if len(pd['options']) > 1:
                for pdo in pd['options']:
                    try:
                        var = item.variations.get(value__icontains=json.dumps(pdo['productDefinitionOptionName']))
                    except ItemVariation.DoesNotExist:
                        var = ItemVariation(item=item)

                    var.value = LazyI18nString({language: pdo['productDefinitionOptionName']})
                    var.default_price = self._money_conversion(event.currency, pdo.get('price', 0))
                    var.save()

                    quota = event.quotas.get_or_create(name=str(item.name) + ' ' + pdo['productDefinitionOptionName'])[
                        0]
                    quota.size = pdo.get('available')  # todo: add already sold ones
                    quota.save()
                    quota.items.add(item)
                    quota.variations.add(var)

            else:
                quota = event.quotas.get_or_create(name=str(item.name))[0]
                quota.size = pd.get('available')  # todo: add already sold ones
                quota.save()
                quota.items.add(item)

        if addon_category:
            for item in admission_items:
                item.addons.update_or_create(
                    addon_category=addon_category,
                    defaults=dict(
                        min_count=0,
                        max_count=len(addon_items),
                    )
                )

    def _import_userdata_definitions(self, event, language, event_id, admission_items):
        userdatas = self.client._get(f'event/{event_id}/userData')['userData']

        for ud in userdatas:
            try:
                question = event.questions.get(identifier=f'xing:{ud["fieldId"]}')
            except Question.DoesNotExist:
                question = Question(event=event, identifier=f'xing:{ud["fieldId"]}')

            if ud['type'] in ("string", "email", "url"):
                question.type = Question.TYPE_STRING
            elif ud['type'] in "textarea":
                question.type = Question.TYPE_TEXT
            elif ud['type'] in ("date", "birthday"):
                question.type = Question.TYPE_DATE
            elif ud['type'] == "datetime":
                question.type = Question.TYPE_DATETIME
            elif ud['type'] == "radio":
                question.type = Question.TYPE_CHOICE
            elif ud['type'] == "checkbox":
                question.type = Question.TYPE_BOOLEAN
            elif ud['type'] == "dropdown":
                question.type = Question.TYPE_CHOICE
            elif ud['type'] == "photo":
                question.type = Question.TYPE_FILE
                question.valid_file_portrait = True
            elif ud['type'] == "file":
                question.type = Question.TYPE_FILE
            elif ud['type'] == "gender":
                question.type = Question.TYPE_CHOICE
            elif ud['type'] == "address":
                question.type = Question.TYPE_TEXT
            elif ud['type'] == "phone":
                question.type = Question.TYPE_PHONENUMBER
            elif ud['type'] == "country":
                question.type = Question.TYPE_COUNTRYCODE
            elif ud['type'] in ("separator", "product", "agb", "privacy"):
                continue
            else:
                question.type = Question.TYPE_STRING

            question.question = ud['title']
            question.required = ud['required']
            question.position = ud.get('orderNumber', 1)
            question.save()
            question.items.set(admission_items)

            if ud['type'] == "gender":
                question.options.update_or_create(identifier='xing:gender:m', defaults={'answer': LazyI18nString({'en': 'male', 'de': 'männlich'})})
                question.options.update_or_create(identifier='xing:gender:f', defaults={'answer': LazyI18nString({'en': 'female', 'de': 'weiblich'})})
                question.options.update_or_create(identifier='xing:gender:x', defaults={'answer': LazyI18nString({'en': 'other', 'de': 'sonstiges'})})
            elif ud['type'] in ('radio', 'dropdown'):
                for udo in ud['options']:
                    question.options.update_or_create(
                        identifier=f'xing:{udo["userDataOptionKey"]}',
                        defaults={'answer': LazyI18nString({language: udo["userDataOptionName"]})}
                    )

    def _import_payments(self, event, language, event_id):
        ids = self.client._get(f'event/{event_id}/payments')['payments']
        for payment_id in ids:
            self._import_payment(event, language, event_id, payment_id)

    def _import_payment(self, event, language, event_id, payment_id):
        payment = self.client._get(f'payment/{payment_id}')['payment']

        order_code = payment["identifier"][-15:]

        if Order.objects.filter(code=order_code).exists():
            return

        payment_products = self.client._get(f'payment/{payment_id}/products')['products']
        ticket_ids = self.client._get(f'payment/{payment_id}/tickets')['tickets']
        tickets = [self.client._get(f'ticket/{ticket_id}')['ticket'] for ticket_id in ticket_ids]
        prop_import_id_ticket = event.item_meta_properties.get_or_create(name="XING-Events-Ticketkategorie")[0]
        prop_import_id_product = event.item_meta_properties.get_or_create(name="XING-Events-Produkt")[0]

        total = self._money_conversion(event.currency, payment["amount"])
        order = Order(
            code=order_code,
            event=event,
            testmode=event.testmode,
            datetime=event.timezone.localize(parse(payment["creationTime"])),
            email_known_to_work=payment["doubleOptIn"] not in ("FALSE", "WAITING"),
            meta_info=json.dumps({
                "xing_import": {
                    "distributionChannel": payment.get("distributionChannel"),
                    "applicationData": payment.get("applicationData"),
                    "type": payment.get("type"),
                    "paymentAuthLoginType": payment.get("paymentAuthLoginType"),
                    "paymentAuthProfileUrl": payment.get("paymentAuthProfileUrl"),
                    "paymentAuthProfileId": payment.get("paymentAuthProfileId"),
                }
            }),
            locale=payment.get("language", language),
            status={
                "new": Order.STATUS_PENDING,
                "authorized": Order.STATUS_PENDING,
                "paid": Order.STATUS_PAID,
                "disbursed": Order.STATUS_PAID,
                "cancelled": Order.STATUS_CANCELED,
            }[payment["status"]],
            total=total,
        )

        order.save()
        ia = InvoiceAddress(order=order)

        positions = []
        fees = []

        for ticket in tickets:
            ticket_products = self.client._get(f'ticket/{ticket["id"]}/products')['products']
            participant = self.client._get(f'participant/{ticket["participantId"]}')['participant']

            if participant["status"] == "com.amiando.participant.status.onHold" and order.status != Order.STATUS_CANCELED:
                order.require_approval = True
                order.status = Order.STATUS_PENDING
                order.save()

            op = OrderPosition(order=order, positionid=len(positions) + 1)
            op.item = ItemMetaValue.objects.get(
                property=prop_import_id_ticket,
                value=str(ticket["ticketCategoryIds"][0]),
                item__event=event,
            ).item
            op.secret = ticket["identifier"]
            op.pseudonymization_id = ticket["displayIdentifier"]
            op.attendee_name_parts = {
                "_scheme": "salutation_given_family",
                "saludation": {
                    0: 'Mr',
                    1: 'Ms',
                    -1: 'Mx',
                    None: '',
                }[ticket.get("salutation")],
                "given_name": ticket.get("firstName") or "",
                "familyName": ticket.get("lastName") or "",
            }
            op.attendee_email = ticket.get("email")
            op.company = ticket.get("company")

            op.price = self._money_conversion(event.currency, ticket.get("originalPrice", 0)) - self._money_conversion(event.currency, ticket.get("discountAmount", 0))

            if ticket.get("cancelled") or participant["status"] in ("com.amiando.participant.status.cancelled", "com.amiando.participant.status.declined"):
                op.canceled = True  # todo: test this

            op.save()
            positions.append(op)

            if participant.get("referenceNumber") and not ia.internal_reference:
                ia.internal_reference = participant["referenceNumber"]
                ia.save()
            if participant.get("buyerAddress") and not ia.city:
                ia.name_parts = {
                    '_scheme': 'salutation_given_family',
                    'salutation': '',
                    'given_name': participant['buyerAddress'].get('firstName') or '',
                    'family_name': participant['buyerAddress'].get('lastName') or '',
                }
                ia.company = participant['buyerAddress'].get('company') or ''
                ia.street = participant['buyerAddress'].get('street') or ''
                ia.zipcode = participant['buyerAddress'].get('zipCode') or ''
                ia.city = participant['buyerAddress'].get('city') or ''
                ia.country = participant['buyerAddress'].get('country') or 'DE'
                ia.vat_id = participant['buyerAddress'].get('vatId') or ''
                ia.save()
                if participant['buyerAddress'].get('email') and not order.email:
                    order.email = participant['buyerAddress'].get('email')
                    order.save()
                if participant['buyerAddress'].get('telephone') and not order.phone:
                    order.phone = participant['buyerAddress'].get('telephone')
                    order.save()
            elif not order.email:
                order.email = participant.get("email")
                order.save()

            for ud in chain(ticket.get("userData", []), payment.get("userData", [])):
                if ud['type'] in ("separator", "product", "unknown", "agb", "privacy"):
                    continue
                question = event.questions.get(identifier=f'xing:{ud["fieldId"]}')
                qa = QuestionAnswer(question=question, orderposition=op)
                if ud['type'] in ("date", "birthday"):
                    qa.answer = str(parse(ud["value"]).date())
                elif ud['type'] == "datetime":
                    qa.answer = str(event.timezone.localize(parse(ud["value"])))
                elif ud['type'] in ("radio", "dropdown"):
                    opt = question.options.get(identifier=f'xing:{ud["userDataOptionKey"]}')
                    qa.answer = str(opt.answer)
                    qa.save()
                    qa.options.set([opt])
                elif ud['type'] == "checkbox":
                    qa.answer = str(ud['value'])
                elif ud['type'] in ("photo", "file"):
                    r = requests.get(ud['value'])
                    r.raise_for_status()
                    value = ContentFile(r.content)
                    qa.save()
                    qa.file.save(os.path.basename(urlparse(ud["value"]).path), value, save=False)
                    qa.answer = 'file://' + qa.file.name
                elif ud['type'] == "address":
                    qa.answer = (
                        f"{ud['value'].get('firstName', '')} {ud['value'].get('lastName', '')}\n"
                        f"{ud['value'].get('street', '')}\n" 
                        f"{ud['value'].get('zipCode', '')} {ud['value'].get('city', '')}\n"
                        f"{ud['value'].get('country', '')}\n" 
                        f"{ud['value'].get('email', '')}"
                    ).strip()
                else:
                    # if ud['type'] in ("string", "email", "url", "textarea", "gender", "phone", "country"):
                    qa.answer = str(ud["value"])
                qa.save()

            if ticket.get("checked"):
                Checkin.objects.create(
                    position=op,
                    datetime=event.timezone.localize(parse(ticket.get("lastChecked"))),
                    list=event.checkin_lists.get_or_create(name="Default")[0]
                )

            for prod in ticket_products:
                opa = OrderPosition(order=order, addon_to=op, canceled=op.canceled, positionid=len(positions) + 1)
                opa.item = ItemMetaValue.objects.get(
                    property=prop_import_id_product,
                    value=str(prod["productCategoryId"]),
                    item__event=event,
                ).item
                opa.variation = opa.item.variations.get(value__icontains=json.dumps(prod['productCategoryOptionName'])) if opa.item.variations.exists() else None
                opa.price = opa.variation.default_price if opa.variation else opa.item.default_price
                opa.save()
                positions.append(opa)

                if prod.get("checked"):
                    Checkin.objects.create(
                        position=opa,
                        list=event.checkin_lists.get_or_create(name="Default")[0]
                    )

        for prod in payment_products:
            opp = OrderPosition(order=order, positionid=len(positions) + 1)
            opp.item = ItemMetaValue.objects.get(
                property=prop_import_id_product,
                value=str(prod["productCategoryId"]),
                item__event=event,
            ).item
            opp.variation = opp.item.variations.get(value__icontains=json.dumps(
                prod['productCategoryOptionName'])) if opp.item.variations.exists() else None
            opp.price = opp.variation.default_price if opp.variation else opa.item.default_price
            opp.save()
            positions.append(opp)

            if prod.get("checked"):
                Checkin.objects.create(
                    position=opp,
                    list=event.checkin_lists.get_or_create(name="Default")[0]
                )

        subtotal = sum(op.price for op in positions) + sum(f.value for f in fees)
        if subtotal != total:
            f = OrderFee(order=order)
            f.fee_type = OrderFee.FEE_TYPE_OTHER
            f.description = 'Differenz zu XING-Buchung'
            f.value = total - subtotal
            f.tax_rule = event.tax_rules.get()
            f.save()
            fees.append(f)
        order.total = sum(op.price for op in positions if not op.canceled) + sum(f.value for f in fees if not f.canceled)
        order.save()

        order.create_transactions(is_new=True, positions=positions, fees=fees)

        if order.status == Order.STATUS_PAID:
            order.payments.create(
                amount=order.total, provider='manual', state=OrderPayment.PAYMENT_STATE_CONFIRMED,
                payment_date=now()
            )

    def _import_code_definitions(self, event, language, event_id):
        prop_import_id_ticket = event.item_meta_properties.get_or_create(name="XING-Events-Ticketkategorie")[0]
        code_def_ids = self.client._get(f'event/{event_id}/codeDefinitions')['codeDefinitions']
        for code_def_id in code_def_ids:
            code_def = self.client._get(f'codeDefinition/{code_def_id}')['codeDefinition']

            valid_until = event.timezone.localize(parse(code_def['endDate'])) if code_def.get('endDate') else None
            if code_def.get('categories', []):
                if len(code_def['categories']) == 1:
                    item = ItemMetaValue.objects.get(
                        property=prop_import_id_ticket,
                        value=str(code_def["categories"][0]),
                        item__event=event,
                    ).item
                    quota = None
                    if code_def['type'] == 'DISCOUNTCODE_TYPE_CATEGORY':
                        item.hide_without_voucher = True
                        item.save()
                else:
                    q = event.quotas.get_or_create(size=None, name=f'Voucher: {code_def["name"]}')[0]
                    items = [
                        mv.item for mv in ItemMetaValue.objects.get(
                            property=prop_import_id_ticket,
                            value__in=[str(c) for c in code_def["categories"]],
                            item__event=event,
                        )
                    ]
                    q.items.set(items)
                    quota = q
                    item = None
                    if code_def['type'] == 'DISCOUNTCODE_TYPE_CATEGORY':
                        for i in items:
                            i.hide_without_voucher = True
                            i.save()

            page_num = 0
            while True:
                r_codes = self.client._get(f'codeDefinition/{code_def_id}/codes?page={page_num}')

                for code in r_codes['codes']:
                    try:
                        v = event.vouchers.get(code=code['code'])
                    except Voucher.DoesNotExist:
                        v = Voucher(event=event, code=code['code'])

                    v.redeemed = code['used']
                    v.tag = code_def['name']
                    v.item = item
                    v.quota = quota
                    v.max_usages = code_def.get('validCount') or 10_000_000
                    v.valid_until = valid_until

                    if code_def['type'] == 'DISCOUNTCODE_TYPE_PERCENT':
                        v.price_mode = 'percent'
                        v.value = Decimal(code_def['value']) / Decimal('100.00')
                        v.show_hidden_items = False
                    elif code_def['type'] == 'DISCOUNTCODE_TYPE_ABSOLUTE':
                        v.price_mode = 'subtract'
                        v.value = self._money_conversion(event.currency, code_def['value'])
                        v.show_hidden_items = False
                    elif code_def['type'] == 'DISCOUNTCODE_TYPE_CATEGORY':
                        v.price_mode = 'none'
                        v.show_hidden_items = True

                    v.save()

                if r_codes['currentPage'] == r_codes['lastPage']:
                    break
                else:
                    page_num += 1
