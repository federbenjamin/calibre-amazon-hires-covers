#!/usr/bin/env python
# Tests for the Kindle hi-res covers plugin.
#
# Run under calibre's Python (it provides lxml, six, calibre and the `_`
# builtin that the plugin relies on):
#
#   /Applications/calibre.app/Contents/MacOS/calibre-debug -e test_plugin.py
#
# The network (browser) and calibre's Amazon source (metadata_plugins) are
# stubbed, so the tests are deterministic and never touch the internet.

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
SCRM = 'https://ec2.images-amazon.com/images/P/%s.01.MAIN._SCRM_.jpg'


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
    """One simulated HTTP fetch: a body, an optional post-redirect URL, or an
    error raised from open() (mimicking mechanize's HTTP 503/403 etc.)."""
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
    """Stands in for the module-level browser(); hands out FakeBrowsers in order
    and counts calls so tests can assert the network was (not) used."""
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


def use_browsers(browsers=None, always=None):
    factory = BrowserFactory(browsers=browsers, always=always)
    khrc.browser = factory
    return factory


def use_amazon(plugins):
    calibre_ui.metadata_plugins = lambda caps=None: plugins


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


# --- #1: ISBN -> ASIN helpers ------------------------------------------------
def test_isbn_to_asin():
    eq('isbn13->isbn10', khrc.isbn_to_asin('9780441013593'), '0441013597')
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


# --- open_url: the boundary that was crashing --------------------------------
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


# --- search_edition_goodreads ------------------------------------------------
def test_search_edition_goodreads():
    log = FakeLog()
    search_url = khrc.urljoin(GOODREADS, '/search?q=neuromancer')

    use_browsers(always=FakeBrowser(error=HTTP_503))
    eq('503 -> None', khrc.search_edition_goodreads(log, 'neuromancer'), None)

    redirect = GOODREADS + '/book/show/999'
    use_browsers([FakeBrowser(body='whatever', final_url=redirect)])
    eq('redirect -> edition', khrc.search_edition_goodreads(log, 'neuromancer'), redirect)

    page = '<html><body><div itemtype="http://schema.org/Book">' \
           '<a itemprop="url" href="/book/show/555">x</a></div></body></html>'
    use_browsers([FakeBrowser(body=page)])  # geturl() == opened url == search_url
    eq('first result -> edition', khrc.search_edition_goodreads(log, 'neuromancer'),
       GOODREADS + '/book/show/555')

    use_browsers([FakeBrowser(body='')])
    eq('empty -> None', khrc.search_edition_goodreads(log, 'neuromancer'), None)


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


# --- #2: search_asins_amazon -------------------------------------------------
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


# --- get_cover_urls: end-to-end fallback behaviour ---------------------------
def test_get_cover_urls():
    log = FakeLog()

    # The reported crash: Goodreads id present, Goodreads returns HTTP 503.
    # Must NOT raise, and must fall through to the ISBN-derived ASIN (#1).
    b = use_browsers(always=FakeBrowser(error=HTTP_503))
    use_amazon([])  # isolate #1 by making #2 yield nothing
    urls = khrc.get_cover_urls(log, 'Neuromancer', ['William Gibson'],
                               {'goodreads': '8812697', 'isbn': '9780441013593'}, 5)
    eq('503 falls back to ISBN-10', urls, [SCRM % '0441013597'])
    check('goodreads was attempted', b.calls >= 1)

    # ASIN already in metadata -> used directly, no network at all.
    b = use_browsers(always=FakeBrowser(error=AssertionError('should not be called')))
    use_amazon([FakeAmazonSource(asins=['SHOULD_NOT'])])
    urls = khrc.get_cover_urls(log, 'T', ['A'], {'amazon': 'B00X57B4JG'}, 5)
    eq('metadata asin used directly', urls, [SCRM % 'B00X57B4JG'])
    eq('no network for metadata asin', b.calls, 0)

    # No id, no ISBN: resolve via the Amazon source (#2).
    b = use_browsers(always=FakeBrowser(error=AssertionError('should not be called')))
    use_amazon([FakeAmazonSource(asins=['B00X57B4JG'])])
    urls = khrc.get_cover_urls(log, 'Neuromancer', ['William Gibson'], {}, 5)
    eq('amazon source resolves asin', urls, [SCRM % 'B00X57B4JG'])
    eq('no goodreads when amazon wins', b.calls, 0)

    # Goodreads id works -> true Kindle ASIN, Amazon source not consulted.
    details = '<html><body><div class="otherEditionsLink">' \
              '<a href="/work/editions/123">All</a></div></body></html>'
    editions = '<html><body><div class="dataValue">B00X57B4JG</div></body></html>'
    use_browsers([FakeBrowser(body=details), FakeBrowser(body=editions)])
    use_amazon([FakeAmazonSource(asins=['SHOULD_NOT'])])
    urls = khrc.get_cover_urls(log, 'T', ['A'], {'goodreads': '8812697'}, 5)
    eq('goodreads id resolves kindle asin', urls, [SCRM % 'B00X57B4JG'])


def main():
    tests = [
        test_isbn_to_asin, test_is_asin, test_is_kindle_asin, test_parse_response,
        test_open_url, test_search_edition_goodreads, test_search_asins_goodreads,
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
