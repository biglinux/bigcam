"""gettext domain used by Python, including plural and contextual messages."""
import gettext
import locale
import os

APP_NAME = "bigcam"
_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_share_dir = os.path.dirname(os.path.dirname(_app_dir))
localedir = os.path.join(_share_dir, "locale")
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass
gettext.bindtextdomain(APP_NAME, localedir)
gettext.textdomain(APP_NAME)
_translation = gettext.translation(APP_NAME, localedir=localedir, fallback=True)
_ = _translation.gettext
ngettext = _translation.ngettext
pgettext = _translation.pgettext
