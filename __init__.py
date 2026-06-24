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
    version                 = (0, 5, 2)
    can_get_multiple_covers = True

    KEY_MAX_COVERS = 'max_covers'

    options = (Option(KEY_MAX_COVERS, 'number', 2, _('Maximum number of covers to get'),
                      _('The maximum number of covers to get from amazon.com (since we try to get the covers from 2 sources, you might end up with two versions of each retrieved cover).')),
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

def get_cover_urls(log, title, authors, identifiers, timeout, abort=None):
    if abort is None:
        abort = Event()
    sources = frozenset([
        'https://ec2.images-amazon.com/images/P/{0}.01.MAIN._SCRM_.jpg',
        # No longer seem to work as of 2024-02-04:
        #'http://z2-ec2.images-amazon.com/images/P/{0}.01.MAIN._SCRM_.jpg',
        #'https://s3.cn-north-1.amazonaws.com.cn/sitbweb-cn/content/{0}/images/cover.jpg',
    ])
    urls = set()
    asins = set()
    # check for ASINs identifiers first
    for id, value in iteritems(identifiers):
        is_asin_id = id == 'asin' or id == 'mobi-asin' or id.startswith('amazon')
        is_kindle  = is_kindle_asin(value)
        if is_asin_id and is_kindle:
            log.info('ASIN present in metadata: %s' % value)
            asins.add(value)
    # otherwise use Goodreads id to find Kindle editions
    if not asins:
        log.info('ASIN not present in metadata')
        goodreads = identifiers.get('goodreads')
        if goodreads:
            log.info('Goodreads id present in metadata: %s' % goodreads)
            asins = search_asins_goodreads(log, goodreads)
        else:
            log.info('Goodreads id not present in metadata')
    # otherwise resolve the ASIN without relying on Goodreads scraping:
    #  #1 the Amazon ASIN of a print book is its ISBN-10, so derive it directly
    #  #2 ask calibre's maintained Amazon source to resolve title/ISBN -> ASIN
    # Both candidates are unioned; the download step validates each cover URL and
    # discards misses (Amazon serves a placeholder image for unknown ASINs).
    if not asins:
        isbn = identifiers.get('isbn')
        if isbn:
            asin = isbn_to_asin(isbn)
            if asin:
                log.info('Derived ASIN from ISBN: %s' % asin)
                asins.add(asin)
        else:
            log.info('ISBN not present in metadata')
        amazon_asins = search_asins_amazon(log, title, authors, identifiers, timeout, abort)
        if amazon_asins:
            log.info('ASINs from Amazon source: %s' % ', '.join(sorted(amazon_asins)))
            asins |= amazon_asins
    # last resort: search Goodreads by ISBN or title+author (often blocked now)
    if not asins:
        isbn = identifiers.get('isbn')
        if isbn:
            query = isbn
        else:
            query = ''
            if title:
                query = title
                if authors and authors[0]:
                    query = query + ' ' + authors[0]
        if query:
            edition = search_edition_goodreads(log, query)
            if edition:
                asins = search_asins_goodreads(log, edition)
    # now convert all ASINs into the download URLs
    if asins:
        for asin in asins:
            for source in sources:
                url = source.format(asin)
                urls.add(url)
    return list(urls)

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

def search_edition_goodreads(log, query):
    edition_url = None

    br = browser()
    search_url = urljoin(goodreads_url, '/search?q=' + quote_plus(query))
    log.info('Searching Goodreads for book: %s' % search_url)
    with closing(br.open(search_url)) as response:
        url = br.geturl()
        if url == search_url:
            # There was no perfect match, get the first result
            doc = parse_response(log, response)
            if doc is None:
                return None
            edition_url = None
            books = doc.xpath('//*[@itemtype="http://schema.org/Book"]')
            if books:
                book = books[0]
                url = book.xpath('.//*[@itemprop="url"]/@href')[0]
                if url:
                    edition_url = url
        else:
            # There was a perfect match and we were redirected to the edition
            edition_url = url

    if edition_url:
        edition_url = urljoin(goodreads_url, edition_url)
    return edition_url

def search_asins_goodreads(log, edition):
    try:
        number = int(edition)
        edition_url = '/book/show/' + str(number)
    except ValueError:
        edition_url = edition
    edition_url = urljoin(goodreads_url, edition_url)

    br = browser()
    # parse the details page and get the link to list all editions
    log.info('Fetching book details: %s' % edition_url)
    with closing(br.open(edition_url)) as response:
        doc = parse_response(log, response)
        if doc is None:
            return set()
        editions_url = doc.xpath('//div[@class="otherEditionsLink"]/a/@href')
        if editions_url:
            editions_url = editions_url[0]
        else:
            return set()
        editions_url = urljoin(goodreads_url, editions_url)

    # list all Kindle editions of the book
    editions_url = urljoin(editions_url, '?expanded=true&filter_by_format=Kindle+Edition&per_page=100')
    log.info('Fetching Kindle editions: %s' % editions_url)
    with closing(br.open(editions_url)) as response:
        doc = parse_response(log, response)
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

