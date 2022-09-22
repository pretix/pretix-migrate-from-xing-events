from django.core.management.base import BaseCommand
from django_scopes import scope

from pretix.base.models import Organizer
from ...importer.main import XINGEventsImporter


class Command(BaseCommand):
    help = 'Imports all data from XING Events'

    def add_arguments(self, parser):
        parser.add_argument('--organizer', type=str, help='Organizer slug')
        parser.add_argument('--apikey', type=str, help='API Key')

        parser.add_argument('--testmode', action='store_true')
        parser.add_argument('--debug', action='store_true')
        parser.add_argument('--verbose', action='store_true')

    def handle(self, *args, **options):
        debug = options['debug']
        verbose = debug or options['verbose']
        organizer = Organizer.objects.get(slug=options['organizer'])

        with scope(organizer=organizer):
            importer = XINGEventsImporter(apikey=options['apikey'], organizer=organizer)
            for event_id in importer.client.get_event_ids():
                importer.import_event(event_id)
