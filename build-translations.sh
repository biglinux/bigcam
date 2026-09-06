#!/usr/bin/env bash
# Compile locale/*.po into the message catalogues the application loads.
#
# The .po files are source and must never be installed: /usr/share/locale is a
# shared namespace and a flat /usr/share/locale/pt-BR.po collides with every
# other package that drops one there.  Only the compiled catalogue belongs in
# the filesystem, and it is namespaced by the text domain:
#
#     /usr/share/locale/<lang>/LC_MESSAGES/bigcam.mo
#
# Weblate-style language tags use a hyphen (pt-BR); gettext looks the
# catalogue up by the POSIX locale name, which uses an underscore (pt_BR), so
# the tag is translated on the way in.

set -euo pipefail

DOMAIN="bigcam"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/locale"
dest="$here/usr/share/locale"

if ! command -v msgfmt >/dev/null 2>&1; then
    echo "msgfmt not found — install gettext" >&2
    exit 1
fi

shopt -s nullglob
po_files=("$src"/*.po)
if [ ${#po_files[@]} -eq 0 ]; then
    echo "no .po files in $src" >&2
    exit 1
fi

built=0
failed=0
for po in "${po_files[@]}"; do
    tag="$(basename "$po" .po)"
    lang="${tag//-/_}"
    out="$dest/$lang/LC_MESSAGES/$DOMAIN.mo"
    mkdir -p "$(dirname "$out")"
    if msgfmt --check-format -o "$out" "$po"; then
        built=$((built + 1))
    else
        echo "failed to compile $po" >&2
        failed=$((failed + 1))
    fi
done

echo "compiled $built catalogue(s) into $dest"
[ "$failed" -eq 0 ]
