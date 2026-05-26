import logging
from typing import Dict
from urllib.parse import urlparse, parse_qs

import discord

from utils.url_utils import extract_markdown_links, extract_plain_links, normalize_url


def collect_unique_links_from_embed(embed: discord.Embed) -> Dict[str, str]:
    unique_links: Dict[str, str] = {}

    def add_link(url: str):
        if not ("/Connections" in url or "Players/Info" in url or "Bans/Hits" in url):
            return
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        qs = parse_qs(parsed.query)
        if "search" in qs:
            search_value = qs.get("search", [""])[0]
            key = f"search:{search_value}"
        elif "connection" in qs:
            connection_value = qs.get("connection", [""])[0]
            key = f"connection:{connection_value}"
        else:
            key = parsed.path
        if key not in unique_links:
            unique_links[key] = normalized
            logging.debug(f"Collected link: {normalized} with key: {key}")

    for field in embed.fields:
        # if field.name.lower() == "name":
        #     continue
        if field.value == "[Unknown](https://admin.deadspace14.net/Connections?showSet=true&search=Unknown&showAccepted=true&showBanned=true&showWhitelist=true&showFull=true&showPanic=true)":
            continue
        if field.value:
            for url in extract_markdown_links(field.value):
                add_link(url)
            for url in extract_plain_links(field.value):
                add_link(url)
    if not unique_links and embed.description:
        for url in extract_plain_links(embed.description):
            add_link(url)
    return unique_links
