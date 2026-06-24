import re
from calibre import as_unicode
from calibre import browser
from calibre.utils.cleantext import clean_ascii_chars
from calibre.ebooks.metadata.sources.base import Source, Option
from contextlib import closing
from lxml.html import fromstring
from lxml.etree import ParserError
from six import iteritems
from six.moves.queue import Queue, Empty
from six.moves.urllib.parse import urljoin, quote_plus
from threading import Event

class KindleHighResCovers(Source):
    name                    = 'Kindle hi-res covers'
    description             = 'Downloads high resolution covers for Kindle editions from Amazon'
    capabilities            = frozenset(['cover'])
    author                  = 'Leonardo Brondani Schenkel <leonardo@schenkel.net>'
    version                 = (0, 6, 0)
    can_get_multiple_covers = True

    KEY_MAX_COVERS = 'max_covers'

    options = (Option(KEY_MAX_COVERS, 'number', 2, _('Maximum number of covers to get'),
                      _('The maximum number of covers to get from amazon.com.')),
    )

    def download_cover(self, log, result_queue, abort,
                       title=None, authors=None, identifiers={},
                       timeout=60, get_best_cover=False):
        urls = get_cover_urls(log, title, authors, identifiers, timeout, abort)
        log.info('Cover URLs: ' + repr(urls))

        if urls:
            log.info('Create link to download cover')
            self.download_multiple_covers(title, authors, urls, False,
                                          timeout, result_queue, abort, log,
                                          None)

# Amazon's hi-res cover endpoint. {0} is the ASIN. The _SCLZZZZZZZ_ size code
# asks for the largest available rendition. Two interchangeable CDN hostnames
# are tried in order so a single unreachable host doesn't lose the cover.
# (The old ec2.images-amazon.com/...MAIN._SCRM_.jpg endpoint now returns a
# 43-byte 1x1 GIF placeholder for everything -- see is_valid_cover.)
COVER_URL_FORMATS = (
    'https://m.media-amazon.com/images/P/{0}.01._SCLZZZZZZZ_.jpg',
    'https://images-na.ssl-images-amazon.com/images/P/{0}.01._SCLZZZZZZZ_.jpg',
)
# Amazon's "no cover" placeholder is a 43-byte 1x1 GIF. Real covers are tens of
# KB of JPEG/PNG, so anything smaller than this is treated as a placeholder.
MIN_COVER_BYTES = 1000

def get_cover_urls(log, title, authors, identifiers, timeout, abort=None):
    if abort is None:
        abort = Event()

    # Gather candidate ASINs from the cheapest/most-reliable sources first.
    asins = []
    def add_asin(value):
        if value and value not in asins:
            asins.append(value)

    # 1. ASIN already in the book's metadata.
    for id, value in iteritems(identifiers):
        is_asin_id = id == 'asin' or id == 'mobi-asin' or id.startswith('amazon')
        if is_asin_id and is_kindle_asin(value):
            log.info('ASIN present in metadata: %s' % value)
            add_asin(value)

    # 2. Goodreads id -> Kindle editions (fails fast and gracefully if blocked).
    if not asins:
        log.info('ASIN not present in metadata')
        goodreads = identifiers.get('goodreads')
        if goodreads:
            log.info('Goodreads id present in metadata: %s' % goodreads)
            for value in search_asins_goodreads(log, goodreads):
                add_asin(value)
        else:
            log.info('Goodreads id not present in metadata')

    # 3. ISBN -> ASIN directly (Amazon's print ASIN is the ISBN-10). No network.
    isbn = identifiers.get('isbn')
    if isbn:
        asin = isbn_to_asin(isbn)
        if asin:
            log.info('Derived ASIN from ISBN: %s' % asin)
            add_asin(asin)
    else:
        log.info('ISBN not present in metadata')

    # Validate the cheap candidates; keep only ASINs that have a real cover.
    urls = cover_urls_for_asins(log, asins, timeout)

    # 4. Only now, if nothing has a cover, pay for calibre's Amazon source
    #    (it scrapes search engines and is slow/rate-limited).
    if not urls:
        log.info('No cover yet; falling back to calibre Amazon source')
        extra = [a for a in search_asins_amazon(log, title, authors, identifiers, timeout, abort)
                 if a not in asins]
        urls = cover_urls_for_asins(log, extra, timeout)
        asins.extend(extra)

    # 5. Last resort: search Goodreads by ISBN or title+author (often blocked).
    if not urls:
        query = isbn
        if not query and title:
            query = title
            if authors and authors[0]:
                query = query + ' ' + authors[0]
        if query:
            edition = search_edition_goodreads(log, query)
            if edition:
                extra = [a for a in search_asins_goodreads(log, edition) if a not in asins]
                urls = cover_urls_for_asins(log, extra, timeout)

    return urls

def cover_urls_for_asins(log, asins, timeout):
    urls = []
    for asin in asins:
        urls.extend(cover_urls_for_asin(log, asin, timeout))
    return urls

def cover_urls_for_asin(log, asin, timeout):
    # Return the first CDN URL that actually serves a real cover for this ASIN
    # (one per ASIN, so the user isn't handed duplicate covers).
    for fmt in COVER_URL_FORMATS:
        url = fmt.format(asin)
        if is_valid_cover(log, url, timeout):
            log.info('Valid cover: %s' % url)
            return [url]
    log.info('No valid cover for ASIN %s' % asin)
    return []

def is_valid_cover(log, url, timeout):
    # Reject Amazon's placeholder (a 43-byte 1x1 GIF) before handing the URL to
    # calibre. A real cover is a non-trivial JPEG/PNG.
    data = fetch_bytes(log, url, timeout)
    if not data or len(data) < MIN_COVER_BYTES:
        return False
    return data.startswith(b'\xff\xd8\xff') or data.startswith(b'\x89PNG\r\n\x1a\n')

def fetch_bytes(log, url, timeout):
    try:
        br = browser()
        with closing(br.open(url)) as response:
            return response.read()
    except Exception as e:
        log.error('Failed to fetch %s: %s' % (url, e))
        return None

def is_kindle_asin(value):
    return value and len(value) == 10 and value.startswith('B')

def is_asin(value):
    # Any Amazon ASIN: 10 uppercase alphanumerics. Also matches an ISBN-10
    # (which is what Amazon uses as the ASIN for a print edition).
    return bool(value) and re.match(r'^[A-Z0-9]{10}$', value.upper()) is not None

def isbn13_to_isbn10(isbn13):
    core = isbn13[3:12]
    try:
        total = sum((10 - i) * int(d) for i, d in enumerate(core))
    except ValueError:
        return None
    check = (11 - total % 11) % 11
    return core + ('X' if check == 10 else str(check))

def isbn_to_asin(isbn):
    # #1: Amazon's ASIN for a print book is its ISBN-10, which also works as the
    # key for the hi-res cover image URL. Accept an ISBN-10 as-is, convert 978-
    # prefixed ISBN-13s, and give up on anything else (e.g. 979- has no ISBN-10).
    isbn = re.sub(r'[^0-9Xx]', '', isbn or '')
    if len(isbn) == 10:
        return isbn.upper()
    if len(isbn) == 13 and isbn.startswith('978'):
        return isbn13_to_isbn10(isbn)
    return None

def search_asins_amazon(log, title, authors, identifiers, timeout, abort):
    # #2: delegate ASIN resolution to calibre's maintained Amazon metadata
    # source, which handles Amazon's search and anti-bot measures for us.
    try:
        from calibre.customize.ui import metadata_plugins
    except ImportError:
        return set()
    amazon = None
    for plugin in metadata_plugins(['identify']):
        if plugin.name == 'Amazon.com':
            amazon = plugin
            break
    if amazon is None:
        log.error('Amazon metadata source not available')
        return set()

    result_queue = Queue()
    log.info('Resolving ASIN via calibre Amazon source')
    try:
        # identify() is blocking: it returns once all results are queued.
        amazon.identify(log, result_queue, abort, title=title, authors=authors,
                        identifiers=identifiers, timeout=timeout)
    except Exception as e:
        log.error('Amazon source identify failed: %s' % e)
        return set()

    asins = set()
    while True:
        try:
            mi = result_queue.get_nowait()
        except Empty:
            break
        for key, value in iteritems(mi.identifiers or {}):
            is_amazon_id = key == 'asin' or key == 'mobi-asin' or key.startswith('amazon')
            if is_amazon_id and is_asin(value):
                asins.add(value.upper())
    return asins

goodreads_url = 'https://www.goodreads.com'

def parse_response(log, response):
    raw = response.read()
    if not raw or not raw.strip():
        log.error('Goodreads returned an empty response (likely blocked or rate-limited)')
        return None
    try:
        return fromstring(raw)
    except ParserError as e:
        log.error('Failed to parse Goodreads response: %s' % e)
        return None

def open_url(log, url):
    # Open a URL and return (doc, final_url). On any failure -- HTTP errors like
    # 503/403, network errors, timeouts, or an empty/unparseable body -- log and
    # return (None, None) so callers degrade gracefully instead of raising.
    try:
        br = browser()
        with closing(br.open(url)) as response:
            final_url = br.geturl()
            return parse_response(log, response), final_url
    except Exception as e:
        log.error('Failed to fetch %s: %s' % (url, e))
        return None, None

def search_edition_goodreads(log, query):
    search_url = urljoin(goodreads_url, '/search?q=' + quote_plus(query))
    log.info('Searching Goodreads for book: %s' % search_url)
    doc, final_url = open_url(log, search_url)
    if final_url is None:
        return None
    if final_url != search_url:
        # There was a perfect match and we were redirected to the edition
        return urljoin(goodreads_url, final_url)
    # There was no perfect match, get the first result
    if doc is None:
        return None
    books = doc.xpath('//*[@itemtype="http://schema.org/Book"]')
    if not books:
        return None
    urls = books[0].xpath('.//*[@itemprop="url"]/@href')
    if not urls:
        return None
    return urljoin(goodreads_url, urls[0])

def search_asins_goodreads(log, edition):
    try:
        edition_url = '/book/show/' + str(int(edition))
    except (ValueError, TypeError):
        edition_url = edition
    edition_url = urljoin(goodreads_url, edition_url)

    # parse the details page and get the link to list all editions
    log.info('Fetching book details: %s' % edition_url)
    doc, _ = open_url(log, edition_url)
    if doc is None:
        return set()
    editions_url = doc.xpath('//div[@class="otherEditionsLink"]/a/@href')
    if not editions_url:
        return set()
    editions_url = urljoin(goodreads_url, editions_url[0])

    # list all Kindle editions of the book
    editions_url = urljoin(editions_url, '?expanded=true&filter_by_format=Kindle+Edition&per_page=100')
    log.info('Fetching Kindle editions: %s' % editions_url)
    doc, _ = open_url(log, editions_url)
    if doc is None:
        return set()
    asins = set()
    for value in doc.xpath('//*[@class="dataValue"]//text()'):
        value = value.strip()
        if is_kindle_asin(value):
            asins.add(value)
    return asins

import getopt
import logging
import sys
if __name__ == "__main__":
    title = None
    author = None
    identifiers = {
        'asin': None,
        'goodreads': None,
        'isbn': None,
    }
    
    opts, args = getopt.gnu_getopt(sys.argv, '', ['title=', 'author=', 'isbn=', 'asin=', 'goodreads='])
    for o, a in opts:
        if o == '--asin':
            identifiers['asin'] = a
        elif o == '--author':
            author = a
        elif o == '--goodreads':
            identifiers['goodreads'] = a
        elif o == '--isbn':
            identifiers['isbn'] = a
        elif o == '--title':
            title = a
   
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    log = logging.getLogger()
    urls = get_cover_urls(log, title, [ author ], identifiers, 60)
    for url in urls:
        print(url)

