from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _

from pretix.control.signals import nav_organizer


@receiver(nav_organizer, dispatch_uid="migrate_from_xing_nav_organizer")
def organizer_nav(sender, request, organizer, **kwargs):
    if request.user.has_organizer_permission(organizer, 'can_change_organizer_settings', request):
        url = resolve(request.path_info)
        return [
            {
                'label': _('Migrate from XING Events'),
                'url': reverse('plugins:pretix_migrate_from_xing_events:index',
                               kwargs={'organizer': organizer.slug}),
                'active': (url.namespace == 'plugins:pretix_migrate_from_xing_events'),
                'icon': 'magic'
            }
        ]
    return []
