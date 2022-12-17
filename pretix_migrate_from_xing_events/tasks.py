from pretix.base.services.orderimport import DataImportError
from pretix.base.services.tasks import OrganizerUserTask
from pretix.celery_app import app
from pretix_migrate_from_xing_events.importer.main import XINGEventsImporter


@app.task(base=OrganizerUserTask, throws=(DataImportError, ImportError,), bind=True)
def import_from_xing(organizer, events, with_vouchers, with_orders, user):
    importer = XINGEventsImporter(
        apikey=organizer.settings.pretix_migrate_from_xing_events_apikey,
        organizer=organizer,
    )
    slugs = []
    for i, event_id in enumerate(events):
        e = importer.import_event(int(event_id), with_vouchers=with_vouchers, with_orders=with_orders)
        slugs.append(e.slug)
    return slugs
