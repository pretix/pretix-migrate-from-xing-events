all: localecompile
LNGS:=`find pretix_migrate_from_xing_events/locale/ -mindepth 1 -maxdepth 1 -type d -printf "-l %f "`

localecompile:
	django-admin compilemessages

localegen:
	django-admin makemessages --keep-pot -i build -i dist -i "*egg*" $(LNGS)

.PHONY: all localecompile localegen
