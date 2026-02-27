import gettext
import os
import locale

APP_NAME = "big-digicam"

localedir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "locale")

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
