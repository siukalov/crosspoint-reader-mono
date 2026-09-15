#!/bin/bash

set -e

ui_only=false
ui_size=""
if [ "$#" -gt 0 ]; then
  if [ "$1" != "--ui-only" ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 [--ui-only [8|10|12]]" >&2
    exit 2
  fi
  if [ "$#" -eq 2 ]; then
    case "$2" in
      8|10|12) ui_size="$2" ;;
      *)
        echo "Usage: $0 [--ui-only [8|10|12]]" >&2
        exit 2
        ;;
    esac
  fi
  ui_only=true
fi

cd "$(dirname "$0")"

READER_FONT_STYLES=("Regular" "Italic" "Bold" "BoldItalic")
NOTOSERIF_FONT_SIZES=(12 14 16 18)
NOTOSANS_FONT_SIZES=(12 14 16 18)

if [ "$ui_only" = false ]; then
  for size in "${NOTOSERIF_FONT_SIZES[@]}"; do
    for style in "${READER_FONT_STYLES[@]}"; do
      font_name="notoserif_${size}_$(echo "$style" | tr '[:upper:]' '[:lower:]')"
      font_path="../builtinFonts/source/NotoSerif/NotoSerif-${style}.ttf"
      output_path="../builtinFonts/${font_name}.h"
      python fontconvert.py "$font_name" "$size" "$font_path" --2bit --compress --pnum > "$output_path"
      echo "Generated $output_path"
    done
  done

  for size in "${NOTOSANS_FONT_SIZES[@]}"; do
    for style in "${READER_FONT_STYLES[@]}"; do
      font_name="notosans_${size}_$(echo "$style" | tr '[:upper:]' '[:lower:]')"
      font_path="../builtinFonts/source/NotoSans/NotoSans-${style}.ttf"
      output_path="../builtinFonts/${font_name}.h"
      python fontconvert.py "$font_name" "$size" "$font_path" --2bit --compress --pnum > "$output_path"
      echo "Generated $output_path"
    done
  done
fi

UI_FONT_SIZES=(10 12)
UI_FONT_STYLES=("Regular" "Bold")

# MiniBidi output requires Arabic contextual presentation forms.
ARABIC_INTERVALS=(
  --additional-intervals 0x060C,0x060C  # Arabic comma
  --additional-intervals 0x061B,0x061B  # Arabic semicolon
  --additional-intervals 0x061F,0x061F  # Arabic question mark
  --additional-intervals 0x0621,0x0621  # hamza (non-joining, never shaped)
  --additional-intervals 0x0640,0x0640  # tatweel
  --additional-intervals 0x0660,0x0669  # Arabic-Indic digits
  --additional-intervals 0x06BA,0x06BA  # noon ghunna base (initial/medial keep base cp)
  --additional-intervals 0x06D4,0x06D4  # Urdu full stop
  --additional-intervals 0x06F0,0x06F9  # extended Arabic-Indic digits (Farsi/Urdu)
  --additional-intervals 0xFB56,0xFB59  # peh (Farsi)
  --additional-intervals 0xFB66,0xFB69  # tteh (Urdu)
  --additional-intervals 0xFB7A,0xFB7D  # tcheh (Farsi)
  --additional-intervals 0xFB88,0xFB95  # ddal, jeh, rreh (Urdu), keheh, gaf (Farsi/Urdu)
  --additional-intervals 0xFB9E,0xFB9F  # noon ghunna isolated/final (Urdu)
  --additional-intervals 0xFBA6,0xFBB1  # heh goal, heh doachashmee, yeh barree(+hamza) (Urdu)
  --additional-intervals 0xFBFC,0xFBFF  # farsi yeh (Farsi/Urdu)
  --additional-intervals 0xFE80,0xFEFC  # Presentation Forms-B: core Arabic + Lam-Alef
)

for size in "${UI_FONT_SIZES[@]}"; do
  if [ -n "$ui_size" ] && [ "$size" != "$ui_size" ]; then
    continue
  fi
  for style in "${UI_FONT_STYLES[@]}"; do
    font_name="ubuntu_${size}_$(echo "$style" | tr '[:upper:]' '[:lower:]')"
    font_path="../builtinFonts/source/Ubuntu/Ubuntu-${style}.ttf"
    hebrew_path="../builtinFonts/source/NotoSansHebrew/NotoSansHebrew-${style}.ttf"
    arabic_path="../builtinFonts/source/NotoSansArabic/NotoSansArabic-${style}.ttf"
    viet_path="../builtinFonts/source/Ubuntu/Ubuntu-Vietnamese-${style}.ttf"
    output_path="../builtinFonts/${font_name}.h"
    python fontconvert.py "$font_name" "$size" "$font_path" "$hebrew_path" "$arabic_path" "$viet_path" \
      --2bit --additional-intervals 0x05D0,0x05EA "${ARABIC_INTERVALS[@]}" > "$output_path"
    echo "Generated $output_path"
  done
done

if [ -z "$ui_size" ] || [ "$ui_size" = 8 ]; then
  python fontconvert.py notosans_8_regular 8 \
    ../builtinFonts/source/NotoSans/NotoSans-Regular.ttf \
    ../builtinFonts/source/NotoSansHebrew/NotoSansHebrew-Regular.ttf \
    ../builtinFonts/source/NotoSansArabic/NotoSansArabic-Regular.ttf \
    --2bit --additional-intervals 0x05D0,0x05EA "${ARABIC_INTERVALS[@]}" > ../builtinFonts/notosans_8_regular.h
fi

if [ "$ui_only" = false ]; then
  echo ""
  echo "Running compression verification..."
  python verify_compression.py ../builtinFonts/
fi
