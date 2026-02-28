import gettext
import os
import locale

APP_NAME = "big-digicam"

# When installed: /usr/share/locale
# When developing: <project>/usr/share/locale
_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_share_dir = os.path.dirname(os.path.dirname(_app_dir))
localedir = os.path.join(_share_dir, "locale")

try:
    locale.setlocale(locale.LC_ALL, '')
except locale.Error:
    pass

if os.path.isdir(localedir):
    gettext.bindtextdomain(APP_NAME, localedir)
    gettext.textdomain(APP_NAME)
    _ = gettext.gettext
else:
    def _(msg):
        return msg
