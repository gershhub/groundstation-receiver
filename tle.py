"""Validated TLE downloads and atomic cache updates, shared by receiver utilities."""

import argparse
from collections import namedtuple
import logging
import os
from pathlib import Path
import stat
import tempfile

import requests


DEFAULT_TLE_URL = 'https://celestrak.org/NORAD/elements/gp.php?NAME=NOAA&FORMAT=TLE'
NOAA_SATELLITES = ('NOAA 15', 'NOAA 18', 'NOAA 19')
NOAA_CATALOG_NUMBERS = {'NOAA 15': '25338', 'NOAA 18': '28654', 'NOAA 19': '33591'}
TLEUpdate = namedtuple('TLEUpdate', 'records updated')


class TLEError(ValueError):
    """The source and cache cannot supply the requested satellite records."""


def _validate_line(line, number):
    if (len(line) != 69 or not line.startswith(str(number) + ' ')
            or not line[2:7].isdigit() or not line[68].isdigit()):
        raise TLEError('Invalid TLE line {}'.format(number))
    checksum = sum(int(char) if char.isdigit() else 1 if char == '-' else 0
                   for char in line[:68]) % 10
    if checksum != int(line[68]):
        raise TLEError('Invalid TLE checksum on line {}'.format(number))


def parse_tles(content, identifiers):
    """Require one complete, checksummed three-line set for each exact name."""
    identifiers = tuple(identifiers)
    if not identifiers:
        raise TLEError('No satellite identifiers were provided')
    try:
        lines = [line.rstrip() for line in content.decode('ascii').splitlines()]
    except UnicodeDecodeError as error:
        raise TLEError('TLE response is not ASCII text') from error
    if not lines or len(lines) % 3:
        raise TLEError('Incomplete three-line TLE data')

    records = {}
    for index in range(0, len(lines), 3):
        name = lines[index].strip()
        if name.startswith('0 '):
            name = name[2:].strip()
        if name not in identifiers:
            continue
        if name in records:
            raise TLEError('Duplicate TLE for {}'.format(name))
        line1, line2 = lines[index + 1:index + 3]
        _validate_line(line1, 1)
        _validate_line(line2, 2)
        catalog = line1[2:7]
        if (catalog != line2[2:7]
                or catalog != NOAA_CATALOG_NUMBERS.get(name, catalog)):
            raise TLEError('Mismatched catalog number for {}'.format(name))
        records[name] = [name, line1, line2]

    missing = [name for name in identifiers if name not in records]
    if missing:
        raise TLEError('Missing TLE for {}'.format(', '.join(missing)))
    return records


def _replace_cache(cache_path, content):
    """Replace only after the entire validated download is written and synced."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(cache_path.stat().st_mode) if cache_path.exists() else 0o644
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(cache_path.parent),
                                         prefix='.' + cache_path.name + '.',
                                         delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.chmod(str(temporary_path), mode)
        os.replace(str(temporary_path), str(cache_path))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def refresh_tles(cache_path, identifiers, url=DEFAULT_TLE_URL):
    """Try once; on any download/validation/write failure, use a valid cache."""
    cache_path = Path(cache_path)
    identifiers = tuple(identifiers)
    try:
        # Do not retry or follow redirects: CelesTrak requires reporting non-200s.
        with requests.get(url, timeout=(5, 20), allow_redirects=False) as response:
            if response.status_code != 200:
                raise TLEError('TLE source returned HTTP {}'.format(response.status_code))
            content = response.content
        records = parse_tles(content, identifiers)
        _replace_cache(cache_path, content)
    except (requests.RequestException, OSError, TLEError) as error:
        logging.warning('TLE refresh failed: %s', error)
        try:
            records = parse_tles(cache_path.read_bytes(), identifiers)
        except (OSError, TLEError) as cache_error:
            raise TLEError('TLE refresh failed ({}); no usable cache at {} ({})'.format(
                error, cache_path, cache_error)) from cache_error
        logging.warning('Using cached TLE from %s; orbital data may be outdated', cache_path)
        return TLEUpdate(records, False)
    logging.info('Cached new TLE at %s', cache_path)
    return TLEUpdate(records, True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', required=True, help='Path to the NOAA TLE cache')
    parser.add_argument('--url', default=DEFAULT_TLE_URL, help='TLE source or mirror URL')
    args = parser.parse_args()
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
    try:
        result = refresh_tles(args.cache, NOAA_SATELLITES, args.url)
    except TLEError as error:
        logging.error('%s', error)
        return 1
    # A usable old cache allows the receiver to continue, but is not a successful
    # refresh: expose that distinction to the standalone updater/service.
    return 0 if result.updated else 1


if __name__ == '__main__':
    raise SystemExit(main())
