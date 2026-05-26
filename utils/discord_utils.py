import re
from typing import Optional, Tuple


def parse_discord_message_link(link: str) -> Optional[Tuple[str, str, str]]:
    full_link_pattern = r'https?://(?:www\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)'

    match = re.match(full_link_pattern, link)
    if match:
        return match.groups()

    if link.isdigit():
        return None, None, link

    return None


def extract_message_id(link_or_id: str) -> Optional[str]:
    if not link_or_id:
        return None

    parsed = parse_discord_message_link(link_or_id)
    if parsed:
        return parsed[2]

    return None
