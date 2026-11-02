"""Optional printed store contact and tax registration evidence."""
import re

PHONE = re.compile(r'(?<!\d)(?:\+?1[ .-]?)?\(?([2-9]\d{2})\)?[ .-]+([2-9]\d{2})[ .-]+(\d{4})(?!\d)')
RT = re.compile(r'(?<!\d)(\d{3}[ -]?\d{3}[ -]?\d{3})\s*[- ]?RT\s*(\d{4})(?!\d)', re.I)


def extract_store_contact(text, *, observations=()):
    from .ingest import _store_header_readings, _store_header_lines
    phones, registrations = set(), set()
    for reading in _store_header_readings(text, observations):
        for line in _store_header_lines(reading):
            if re.search(r'\b(?:fax|customer|member|loyalty|order|invoice)\b', line, re.I):
                continue
            for match in PHONE.finditer(line):
                if PHONE.fullmatch(line.strip()) or re.match(r'^(?:phone|telephone|tel)\b', line, re.I):
                    phones.add('-'.join(match.groups()))
        for match in RT.finditer(reading):
            registrations.add(re.sub(r'\D', '', match[1]) + 'RT' + match[2])
    return {'store_phone': next(iter(phones)) if len(phones) == 1 else None,
            'tax_registration_number': next(iter(registrations)) if len(registrations) == 1 else None}
