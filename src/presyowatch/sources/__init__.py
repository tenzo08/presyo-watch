"""Source-specific scraping: index pages, link discovery, date extraction.

Everything here is a pure function over text. The only I/O is the thin wrapper in
:mod:`presyowatch.sources.index` that fetches an index page, so parsers can be tested
against committed fixtures of what the sources really served.
"""
