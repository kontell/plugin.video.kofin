#!/usr/bin/env bash
# Rasterise the downloaded-badge artwork from its Material Symbols sources.
#
# The three PNGs under resources/media are drawn from the SVGs beside this
# script; they shipped at 32x32 greyscale, which was fine for the overlay a
# skin draws at 24px and visibly soft everywhere else — the same file is now
# the icon on the generated Downloaded library nodes, where Kodi hands it a
# 256px slot.
#
# Sources are Material Symbols exports (24px, viewBox="0 -960 960 960",
# fill #e3e3e3); the fill is left alone so the badge keeps matching the rest
# of Kodi's monochrome iconography.
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

if ! command -v inkscape >/dev/null; then
    echo "inkscape is required (ImageMagick's SVG delegate renders these badly)" >&2
    exit 1
fi

echo "rendering download badges into $media"
render "$here/download_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg" \
       "$media/downloaded.png"
render "$here/downloading_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg" \
       "$media/downloaded-resume.png"
render "$here/download_for_offline_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg" \
       "$media/downloaded-watched.png"
echo "done"
