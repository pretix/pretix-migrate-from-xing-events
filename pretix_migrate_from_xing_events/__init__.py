from django.utils.translation import gettext_lazy

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")

__version__ = "1.0.0"


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


default_app_config = "pretix_migrate_from_xing_events.PluginApp"
