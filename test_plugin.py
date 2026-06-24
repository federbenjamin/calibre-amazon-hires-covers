#!/usr/bin/env python
# Tests for the Kindle hi-res covers plugin.
#
# Run under calibre's Python (it provides lxml, six, calibre and the `_`
# builtin that the plugin relies on):
#
#   /Applications/calibre.app/Contents/MacOS/calibre-debug -e test_plugin.py
#
# All network access is stubbed -- browser() for HTML fetches and fetch_bytes()
# for image fetches -- so the tests are deterministic and never hit the network.

import importlib.util
import os
import sys

# --- load the plugin module from __init__.py ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("khrc", os.path.join(_HERE, "__init__.py"))
khrc = importlib.util.module_from_spec(_spec)
sys.modules["khrc"] = khrc
_spec.loader.exec_module(khrc)

import calibre.customize.ui as calibre_ui
from calibre.ebooks.metadata.book.base import Metadata

GOODREADS = khrc.goodreads_url
SCL = 'https://m.media-amazon.com/images/P/%s.01._SCLZZZZZZZ_.jpg'        # primary CDN
SCL2 = 'https://images-na.ssl-images-amazon.com/images/P/%s.01._SCLZZZZZZZ_.jpg'  # fallback CDN

# Real image bodies vs Amazon's 43-byte 1x1 GIF "no cover" placeholder.
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 2000
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 2000
# The exact 43-byte 1x1 GIF Amazon serves when it has no cover for an ASIN.
PLACEHOLDER_GIF = bytes.fromhex(
    '47494638396101000100800100000000ffffff21f90401000001002c00000000010001000002024c01003b')

_ORIG_FETCH_BYTES = khrc.fetch_bytes
_ORIG_FETCH_JSON = khrc.fetch_json


# --- test doubles ------------------------------------------------------------
class FakeLog(object):
    def __init__(self):
        self.lines = []

    def _rec(self, *a):
        self.lines.append(' '.join(str(x) for x in a))

    info = warning = error = exception = debug = _rec

    def __call__(self, *a):  # calibre's Log object is callable
        self._rec(*a)


class FakeResponse(object):
    def __init__(self, body):
        self._body = body.encode('utf-8') if isinstance(body, str) else body

    def read(self):
        return self._body

    def close(self):
        pass


class FakeBrowser(object):
    """One simulated fetch: a body, an optional post-redirect URL, or an error
    raised from open() (mimicking mechanize's HTTP 503/403 etc.)."""
    def __init__(self, body='', final_url=None, error=None):
        self._body, self._final_url, self._error, self._opened = body, final_url, error, None

    def open(self, url):
        self._opened = url
        if self._error is not None:
            raise self._error
        return FakeResponse(self._body)

    def geturl(self):
        return self._final_url if self._final_url is not None else self._opened


class BrowserFactory(object):
    """Stands in for browser(); hands out FakeBrowsers in order and counts calls
    so tests can assert the network was (not) used."""
    def __init__(self, browsers=None, always=None):
        self.queue = list(browsers) if browsers else []
        self.always = always
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.always is not None:
            return self.always
        if not self.queue:
            raise AssertionError('browser() called more times than expected')
        return self.queue.pop(0)


class FakeAmazonSource(object):
    name = 'Amazon.com'

    def __init__(self, asins=(), raises=None):
        self._asins, self._raises = list(asins), raises

    def identify(self, log, result_queue, abort, title=None, authors=None,
                 identifiers=None, timeout=None):
        if self._raises is not None:
            raise self._raises
        for asin in self._asins:
            mi = Metadata('T')
            mi.set_identifier('amazon', asin)
            result_queue.put(mi)


class AmazonSpy(object):
    """Stands in for metadata_plugins(); counts calls."""
    def __init__(self, plugins):
        self.plugins, self.calls = plugins, 0

    def __call__(self, capabilities=None):
        self.calls += 1
        return self.plugins


class JsonSpy(object):
    """Stands in for fetch_json(); returns canned data, records url and calls."""
    def __init__(self, data):
        self.data, self.calls, self.url = data, 0, None

    def __call__(self, log, url, timeout):
        self.calls += 1
        self.url = url
        return self.data


def use_browsers(browsers=None, always=None):
    factory = BrowserFactory(browsers=browsers, always=always)
    khrc.browser = factory
    return factory


def use_amazon(plugins):
    spy = AmazonSpy(plugins)
    calibre_ui.metadata_plugins = spy
    return spy


def use_covers(mapping, default=PLACEHOLDER_GIF):
    """Stub fetch_bytes: any URL containing an ASIN key returns its bytes."""
    def fake(log, url, timeout):
        for asin, data in mapping.items():
            if asin in url:
                return data
        return default
    khrc.fetch_bytes = fake


def use_openlibrary(data):
    spy = JsonSpy(data)
    khrc.fetch_json = spy
    return spy


HTTP_503 = Exception('HTTP Error 503: Service Unavailable')


# --- tiny test runner --------------------------------------------------------
_passed, _failed = 0, 0


def check(name, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print('  ok   %s' % name)
    else:
        _failed += 1
        print('  FAIL %s %s' % (name, detail))


def eq(name, got, expected):
    check(name, got == expected, '(got %r, expected %r)' % (got, expected))


# --- ISBN -> ASIN helpers ----------------------------------------------------
def test_isbn_to_asin():
    eq('isbn13->isbn10', khrc.isbn_to_asin('9780441013593'), '0441013597')
    eq('blood meridian', khrc.isbn_to_asin('9780307762528'), '0307762521')
    eq('isbn13 hyphens', khrc.isbn_to_asin('978-0-13-468599-1'), '0134685997')
    eq('isbn10 passthrough', khrc.isbn_to_asin('0441013597'), '0441013597')
    eq('isbn10 with X', khrc.isbn_to_asin('043935806X'), '043935806X')
    eq('isbn10 lower x upper', khrc.isbn_to_asin('043935806x'), '043935806X')
    eq('979 has no isbn10', khrc.isbn_to_asin('9791234567896'), None)
    eq('garbage', khrc.isbn_to_asin('not-an-isbn'), None)
    eq('empty', khrc.isbn_to_asin(''), None)
    eq('none', khrc.isbn_to_asin(None), None)


def test_is_asin():
    eq('print asin', khrc.is_asin('0441013597'), True)
    eq('kindle asin', khrc.is_asin('B00X57B4JG'), True)
    eq('isbn10 X', khrc.is_asin('043935806X'), True)
    eq('too short', khrc.is_asin('short'), False)
    eq('too long', khrc.is_asin('ABCDEFGHIJK'), False)
    eq('empty', khrc.is_asin(''), False)
    eq('none', khrc.is_asin(None), False)


def test_is_kindle_asin():
    eq('kindle', bool(khrc.is_kindle_asin('B00X57B4JG')), True)
    eq('print not kindle', bool(khrc.is_kindle_asin('0441013597')), False)
    eq('short not kindle', bool(khrc.is_kindle_asin('B123')), False)


# --- parse_response ----------------------------------------------------------
def test_parse_response():
    log = FakeLog()
    eq('empty body -> None', khrc.parse_response(log, FakeResponse('')), None)
    eq('whitespace -> None', khrc.parse_response(log, FakeResponse('   \n ')), None)
    doc = khrc.parse_response(log, FakeResponse('<html><body><p>hi</p></body></html>'))
    check('valid html -> doc', doc is not None and doc.xpath('//p/text()') == ['hi'])


# --- open_url: the HTTP boundary that was crashing ---------------------------
def test_open_url():
    log = FakeLog()

    use_browsers(always=FakeBrowser(error=HTTP_503))
    doc, final = khrc.open_url(log, 'http://x/503')
    check('http 503 -> (None, None)', doc is None and final is None)

    use_browsers([FakeBrowser(body='')])
    doc, final = khrc.open_url(log, 'http://x/empty')
    check('empty body -> (None, url)', doc is None and final == 'http://x/empty')

    use_browsers([FakeBrowser(body='<html><body><b>ok</b></body></html>')])
    doc, final = khrc.open_url(log, 'http://x/ok')
    check('valid -> (doc, url)', doc is not None and doc.xpath('//b/text()') == ['ok'])


# --- fetch_bytes / is_valid_cover: placeholder rejection ---------------------
def test_fetch_bytes():
    khrc.fetch_bytes = _ORIG_FETCH_BYTES  # exercise the real implementation
    log = FakeLog()

    use_browsers([FakeBrowser(body=JPEG)])
    eq('returns image bytes', khrc.fetch_bytes(log, 'http://x/c.jpg', 5), JPEG)

    use_browsers(always=FakeBrowser(error=HTTP_503))
    eq('503 -> None', khrc.fetch_bytes(log, 'http://x/c.jpg', 5), None)


def test_is_valid_cover():
    khrc.fetch_bytes = _ORIG_FETCH_BYTES
    log = FakeLog()

    use_browsers([FakeBrowser(body=PLACEHOLDER_GIF)])
    eq('43-byte gif placeholder -> False', khrc.is_valid_cover(log, SCL % 'X', 5), False)

    use_browsers([FakeBrowser(body=JPEG)])
    eq('real jpeg -> True', khrc.is_valid_cover(log, SCL % 'X', 5), True)

    use_browsers([FakeBrowser(body=PNG)])
    eq('real png -> True', khrc.is_valid_cover(log, SCL % 'X', 5), True)

    use_browsers([FakeBrowser(body=b'\xff\xd8\xff' + b'\x00' * 10)])
    eq('tiny jpeg -> False', khrc.is_valid_cover(log, SCL % 'X', 5), False)

    use_browsers(always=FakeBrowser(error=HTTP_503))
    eq('fetch fails -> False', khrc.is_valid_cover(log, SCL % 'X', 5), False)


# --- cover_urls_for_asin: CDN failover, one url per asin ----------------------
def test_cover_urls_for_asin():
    log = FakeLog()

    use_covers({'AAAA': JPEG})
    eq('primary cdn valid', khrc.cover_urls_for_asin(log, 'AAAA', 5), [SCL % 'AAAA'])

    # Primary host serves a placeholder, fallback host has the real cover.
    def fake(log, url, timeout):
        return JPEG if url.startswith('https://images-na.') else PLACEHOLDER_GIF
    khrc.fetch_bytes = fake
    eq('falls over to second cdn', khrc.cover_urls_for_asin(log, 'BBBB', 5), [SCL2 % 'BBBB'])

    use_covers({})  # every host returns the placeholder
    eq('no valid cover -> []', khrc.cover_urls_for_asin(log, 'CCCC', 5), [])


# --- fetch_json / search_asins_openlibrary -----------------------------------
def test_fetch_json():
    khrc.fetch_json = _ORIG_FETCH_JSON
    khrc.fetch_bytes = _ORIG_FETCH_BYTES
    log = FakeLog()

    use_browsers([FakeBrowser(body=b'{"docs": [{"isbn": ["0307762521"]}]}')])
    eq('parses json', khrc.fetch_json(log, 'http://ol/s', 5), {'docs': [{'isbn': ['0307762521']}]})

    use_browsers([FakeBrowser(body=b'<html>not json</html>')])
    eq('bad json -> None', khrc.fetch_json(log, 'http://ol/s', 5), None)

    use_browsers(always=FakeBrowser(error=HTTP_503))
    eq('fetch fails -> None', khrc.fetch_json(log, 'http://ol/s', 5), None)


def test_search_asins_openlibrary():
    log = FakeLog()

    spy = use_openlibrary({'docs': [{'isbn': ['0307387895', '9780441013593']},
                                    {'isbn': ['043935806X']}]})
    asins = khrc.search_asins_openlibrary(log, 'The Road', ['Cormac McCarthy'], None, 5)
    eq('derives + dedupes asins from isbns', asins, ['0307387895', '0441013597', '043935806X'])
    check('title+author query used', 'title=' in spy.url and 'author=' in spy.url)

    spy = use_openlibrary({'docs': [{'isbn': ['0307762521']}]})
    asins = khrc.search_asins_openlibrary(log, None, None, '9780307762528', 5)
    eq('isbn-only -> q query', asins, ['0307762521'])
    check('q query used', 'q=' in spy.url and 'title=' not in spy.url)

    use_openlibrary(None)
    eq('no json -> []', khrc.search_asins_openlibrary(log, 'X', ['Y'], None, 5), [])

    use_openlibrary({'docs': []})
    eq('no docs -> []', khrc.search_asins_openlibrary(log, 'X', ['Y'], None, 5), [])

    spy = use_openlibrary({'docs': [{'isbn': ['x']}]})
    eq('nothing to query -> []', khrc.search_asins_openlibrary(log, None, None, None, 5), [])
    eq('no query -> no fetch', spy.calls, 0)


# --- search_asins_goodreads --------------------------------------------------
def test_search_asins_goodreads():
    log = FakeLog()
    details = '<html><body><div class="otherEditionsLink">' \
              '<a href="/work/editions/123">All</a></div></body></html>'
    editions = '<html><body><div class="dataValue">B00X57B4JG</div>' \
               '<div class="dataValue">not-an-asin</div></body></html>'

    use_browsers(always=FakeBrowser(error=HTTP_503))
    eq('details 503 -> set()', khrc.search_asins_goodreads(log, '8812697'), set())

    use_browsers([FakeBrowser(body='<html><body>no link</body></html>')])
    eq('no editions link -> set()', khrc.search_asins_goodreads(log, '8812697'), set())

    use_browsers([FakeBrowser(body=details), FakeBrowser(body=editions)])
    eq('happy path -> {asin}', khrc.search_asins_goodreads(log, '8812697'), {'B00X57B4JG'})

    use_browsers([FakeBrowser(body=details), FakeBrowser(error=HTTP_503)])
    eq('editions 503 -> set()', khrc.search_asins_goodreads(log, '8812697'), set())


# --- search_asins_amazon -----------------------------------------------------
def test_search_asins_amazon():
    log = FakeLog()
    from threading import Event

    use_amazon([FakeAmazonSource(asins=['B00X57B4JG', '0441013597'])])
    eq('amazon -> asins', khrc.search_asins_amazon(log, 'T', ['A'], {}, 5, Event()),
       {'B00X57B4JG', '0441013597'})

    use_amazon([])
    eq('no amazon plugin -> set()', khrc.search_asins_amazon(log, 'T', ['A'], {}, 5, Event()), set())

    use_amazon([FakeAmazonSource(raises=Exception('boom'))])
    eq('identify raises -> set()', khrc.search_asins_amazon(log, 'T', ['A'], {}, 5, Event()), set())


# --- get_cover_urls: end-to-end behaviour ------------------------------------
def test_get_cover_urls():
    log = FakeLog()

    # The reported bug: Goodreads 503, ISBN present. Must NOT crash, must emit the
    # working _SCLZZZZZZZ_ url for the ISBN-derived ASIN, and must touch neither
    # OpenLibrary nor the Amazon source because a real cover was already found.
    b = use_browsers(always=FakeBrowser(error=HTTP_503))
    ol = use_openlibrary({'docs': [{'isbn': ['SHOULD_NOT']}]})
    amz = use_amazon([FakeAmazonSource(asins=['SHOULD_NOT'])])
    use_covers({'0307762521': JPEG})
    urls = khrc.get_cover_urls(log, 'Blood Meridian', ['Cormac McCarthy'],
                               {'goodreads': '8812697', 'isbn': '9780307762528'}, 5)
    eq('isbn cover, real url', urls, [SCL % '0307762521'])
    check('goodreads attempted', b.calls >= 1)
    eq('openlibrary NOT called', ol.calls, 0)
    eq('amazon source NOT called', amz.calls, 0)

    # ISBN edition has no cover -> OpenLibrary finds another edition that does,
    # and the Google-scraping Amazon source is never reached.
    use_browsers(always=FakeBrowser(error=HTTP_503))
    ol = use_openlibrary({'docs': [{'isbn': ['0307387895']}]})  # alternate edition
    amz = use_amazon([FakeAmazonSource(asins=['SHOULD_NOT'])])
    use_covers({'0307387895': JPEG})  # 0307762521 -> placeholder (default)
    urls = khrc.get_cover_urls(log, 'Blood Meridian', ['Cormac McCarthy'],
                               {'isbn': '9780307762528'}, 5)
    eq('openlibrary alternate edition cover', urls, [SCL % '0307387895'])
    check('openlibrary WAS called', ol.calls >= 1)
    eq('amazon source NOT reached', amz.calls, 0)

    # Nothing cheap works and OpenLibrary finds nothing -> Amazon source (last
    # resort) supplies a Kindle ASIN with a cover.
    use_browsers(always=FakeBrowser(error=HTTP_503))
    ol = use_openlibrary({'docs': []})
    amz = use_amazon([FakeAmazonSource(asins=['B00X57B4JG'])])
    use_covers({'B00X57B4JG': JPEG})
    urls = khrc.get_cover_urls(log, 'Blood Meridian', ['Cormac McCarthy'],
                               {'isbn': '9780307762528'}, 5)
    eq('amazon source last resort', urls, [SCL % 'B00X57B4JG'])
    check('openlibrary tried first', ol.calls >= 1)
    check('amazon source WAS called', amz.calls >= 1)

    # ASIN already in metadata, real cover -> used directly, no network at all.
    b = use_browsers(always=FakeBrowser(error=AssertionError('no network expected')))
    ol = use_openlibrary({'docs': [{'isbn': ['SHOULD_NOT']}]})
    amz = use_amazon([FakeAmazonSource(asins=['SHOULD_NOT'])])
    use_covers({'B00X57B4JG': JPEG})
    urls = khrc.get_cover_urls(log, 'T', ['A'], {'amazon': 'B00X57B4JG'}, 5)
    eq('metadata asin used directly', urls, [SCL % 'B00X57B4JG'])
    eq('no goodreads for metadata asin', b.calls, 0)
    eq('no openlibrary for metadata asin', ol.calls, 0)
    eq('no amazon source for metadata asin', amz.calls, 0)

    # Goodreads id resolves a Kindle ASIN with a cover -> nothing else consulted.
    details = '<html><body><div class="otherEditionsLink">' \
              '<a href="/work/editions/123">All</a></div></body></html>'
    editions = '<html><body><div class="dataValue">B00X57B4JG</div></body></html>'
    use_browsers([FakeBrowser(body=details), FakeBrowser(body=editions)])
    ol = use_openlibrary({'docs': [{'isbn': ['SHOULD_NOT']}]})
    amz = use_amazon([FakeAmazonSource(asins=['SHOULD_NOT'])])
    use_covers({'B00X57B4JG': JPEG})
    urls = khrc.get_cover_urls(log, 'T', ['A'], {'goodreads': '8812697'}, 5)
    eq('goodreads id resolves cover', urls, [SCL % 'B00X57B4JG'])
    eq('no openlibrary when goodreads wins', ol.calls, 0)
    eq('no amazon source when goodreads wins', amz.calls, 0)

    # Nothing resolves a cover anywhere -> empty list, no crash.
    use_browsers(always=FakeBrowser(error=HTTP_503))
    use_openlibrary(None)
    use_amazon([])
    use_covers({})
    urls = khrc.get_cover_urls(log, 'Unknown', ['Nobody'], {'isbn': '9780307762528'}, 5)
    eq('no cover anywhere -> []', urls, [])


def main():
    tests = [
        test_isbn_to_asin, test_is_asin, test_is_kindle_asin, test_parse_response,
        test_open_url, test_fetch_bytes, test_is_valid_cover, test_cover_urls_for_asin,
        test_fetch_json, test_search_asins_openlibrary, test_search_asins_goodreads,
        test_search_asins_amazon, test_get_cover_urls,
    ]
    for t in tests:
        print('\n%s' % t.__name__)
        try:
            t()
        except Exception as e:
            global _failed
            _failed += 1
            print('  FAIL %s raised %r' % (t.__name__, e))
    print('\n%d passed, %d failed' % (_passed, _failed))
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
