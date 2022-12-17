from django.utils.translation import gettext_lazy
from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    name = "pretix_migrate_from_xing_events"
    verbose_name = "Migration Assistant from XING Events"

    class PretixPluginMeta:
        name = gettext_lazy("Migration Assistant from XING Events")
        author = "pretix"
        description = gettext_lazy("Assists migrating from XING Events to pretix")
        visible = False
        version = __version__
        category = "INTEGRATION"
        compatibility = "pretix>=2.7.0"

    def ready(self):
        from . import signals  # NOQA


