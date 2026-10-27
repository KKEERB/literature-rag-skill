"""Reject characters Zotero would silently discard from scientific notes."""
import re
from pathlib import Path

FORBIDDEN_CONTROLS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
NOTE_ARTIFACTS = ('08_reader_note.zotero.html', '09_research_card.zotero.html')


def validate_note_html(text, *, label='note HTML'):
    match = FORBIDDEN_CONTROLS.search(text)
    if match:
        raise ValueError(
            f'{label}: forbidden U+{ord(match.group()):04X} at character {match.start()}; '
            'verify the original PDF glyph before a versioned correction; do not silently strip it')


def validate_note_artifacts(directory):
    for name in NOTE_ARTIFACTS:
        path = Path(directory) / name
        validate_note_html(path.read_text(encoding='utf-8'), label=str(path))
