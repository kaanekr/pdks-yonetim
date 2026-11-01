"""Generate the built-in firmware web panel from data/index.html.

The panel is served from PROGMEM so a signed application OTA updates the web UI
without touching LittleFS (personnel, settings and attendance stay untouched).
"""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'data/index.html'
HEADER = ROOT/'include/PublicPanel.h'
DELIMITER = 'PDKS_PANEL'


def render():
    html = SOURCE.read_text(encoding='utf-8')
    if f'){DELIMITER}"' in html:
        raise SystemExit('Panel contains the embedding delimiter; choose another delimiter.')
    return ('#pragma once\n#include <pgmspace.h>\n'
            'static const char PDKS_PANEL_HTML[] PROGMEM = R"'
            + DELIMITER + '(' + html + ')' + DELIMITER + '";\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not HEADER.is_file() or HEADER.read_text(encoding='utf-8') != expected:
            raise SystemExit('PublicPanel.h is stale; run tools/embed_panel.py')
        print('Panel embed is current:', len(expected), 'bytes')
        return
    HEADER.write_text(expected, encoding='utf-8')
    print('Wrote', HEADER, len(expected), 'bytes')


if __name__ == '__main__':
    main()
