#!/usr/bin/env bash
# Rasterise Material Symbols sources into resources/media.
#
# The PNGs under resources/media are drawn from the SVGs beside this script.
# Download badges shipped at 32x32 greyscale, which was fine for the overlay a
# skin draws at 24px and visibly soft everywhere else — the same file is now
# the icon on the generated Downloaded library nodes, where Kodi hands it a
# 256px slot. The alphabet listing icon is the same 256px slot.
#
# Sources are Material Symbols exports (24px, viewBox="0 -960 960 960",
# fill #e3e3e3). Download badges keep that fill so they match the rest of
# Kodi's monochrome iconography. Listing icons (alphabet) are refilled white
# to match person-search.png and syncplay-groups.png.
#
# sort_by_alpha uses almost the whole 24dp square, so a 1:1 export reads
# larger than the addon's other listing icons. render_listing expands the
# viewBox so the glyph occupies listing_inner of the 256px canvas.
#
# tools/ is excluded from the shipped zip (tools/build.py), so this is a dev
# utility: run it after replacing an SVG, and commit the PNGs it writes.
#
#   tools/render-icons.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
media="$(cd "$here/.." && pwd)/resources/media"

size=256
listing_inner=224

render() {
    local source="$1" target="$2"
    echo "  $(basename "$source") -> $(basename "$target") (${size}x${size})"
    inkscape \
        --export-type=png \
        --export-width="$size" \
        --export-height="$size" \
        --export-background-opacity=0 \
        --export-filename="$target" \
        "$source" >/dev/null
}

# White fill, extra canvas padding. The Material viewBox is expanded on a
# copy so the original SVG stays the upstream export. Inkscape writes white
# into fully-transparent pixels; zero that so the PNG matches the other
# listing icons (person-search.png / syncplay-groups.png).
render_listing() {
    local source="$1" target="$2"
    local padded
    padded="$(mktemp --suffix=.svg)"
    python3 - "$source" "$padded" "$listing_inner" "$size" <<'PY'
import sys

src, dst, inner, canvas = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
text = open(src, encoding="utf-8").read()
text = text.replace('fill="#e3e3e3"', 'fill="#ffffff"')
vb = 960.0
new = vb * canvas / inner
margin = (new - vb) / 2
text = text.replace(
    'viewBox="0 -960 960 960"',
    f'viewBox="{-margin} {-960 - margin} {new} {new}"',
)
open(dst, "w", encoding="utf-8").write(text)
PY
    echo "  $(basename "$source") -> $(basename "$target") (${size}x${size}, glyph ${listing_inner}px)"
    inkscape \
        --export-type=png \
        --export-width="$size" \
        --export-height="$size" \
        --export-background-opacity=0 \
        --export-filename="$target" \
        "$padded" >/dev/null
    rm -f "$padded"
    python3 - "$target" <<'PY'
import sys
from PIL import Image

path = sys.argv[1]
im = Image.open(path).convert("RGBA")
pixels = im.load()
width, height = im.size
for y in range(height):
    for x in range(width):
        r, g, b, a = pixels[x, y]
        if a == 0 and (r or g or b):
            pixels[x, y] = (0, 0, 0, 0)
im.save(path)
PY
}

if ! command -v inkscape >/dev/null; then
    echo "inkscape is required (ImageMagick's SVG delegate renders these badly)" >&2
    exit 1
fi

echo "rendering icons into $media"
render "$here/download_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg" \
       "$media/downloaded.png"
render "$here/downloading_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg" \
       "$media/downloaded-resume.png"
render "$here/download_for_offline_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg" \
       "$media/downloaded-watched.png"
render_listing "$here/sort_by_alpha_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg" \
       "$media/alphabet.png"
echo "done"
