import logging
import re
from typing import List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote


def extract_markdown_links(text: str) -> List[str]:
    pattern = r'\[.*?\]\(\s*(https?://[^\s\)]+)\s*\)'
    links = re.findall(pattern, text)
    logging.debug(f"Extracted markdown links: {links}")
    return links


def extract_plain_links(text: str) -> List[str]:
    pattern = r'\bhttps?://\S+\b'
    links = re.findall(pattern, text)
    logging.debug(f"Extracted plain links: {links}")
    return links


def normalize_url(url_str: str) -> str:
    try:
        parsed = urlparse(url_str)
        query_params = parse_qs(parsed.query)
        essential_params = ['search', 'connection', 'showSet', 'showAccepted', 'showBanned', 'showWhitelist',
                            'showFull', 'showPanic', 'perPage', 'sort', 'pageIndex']
        filtered_params = {k: v for k, v in query_params.items() if k in essential_params}
        sorted_params = sorted(filtered_params.items())
        encoded_query = urlencode(sorted_params, doseq=True)
        new_parsed = parsed._replace(query=encoded_query)
        normalized = urlunparse(new_parsed)
        logging.debug(f"Normalized URL: {normalized}")
        return normalized
    except Exception as e:
        logging.warning(f"URL normalization failed for '{url_str}': {e}. Returning original URL.")
        return url_str


def extract_effective_search_term(term: str) -> str:
    if term.startswith("http"):
        try:
            parsed = urlparse(term)
            raw_query = parsed.query
            m = re.search(r'(?:^|&)search=([^&]+)', raw_query)
            if m:
                raw_value = m.group(1)
                decoded_value = unquote(raw_value)
                return decoded_value
        except Exception as e:
            logging.warning(f"Error parsing URL '{term}' to extract search term: {e}. Returning original term.")
            return term
    return term
