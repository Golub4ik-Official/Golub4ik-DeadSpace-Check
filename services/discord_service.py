import asyncio
import logging
import re
from typing import List, Dict, Any, Optional

import discord

from models.complaint import ComplaintChannel, ComplaintMessage
from models.message import DiscordMessage
from utils.embed_utils import collect_unique_links_from_embed


class DiscordService:
    def __init__(self, client: discord.Client) -> None:
        self.target_channel_id = None
        self.client = client
        self.target_channel: Optional[discord.TextChannel] = None
        self.complaint_channels: Dict[int, discord.TextChannel] = {}
        self.logger = logging.getLogger(__name__)
        self.max_retries = 3
        self.retry_delay = 2

    async def setup_channels(self, target_channel_id: int, complaint_channel_ids: List[int]) -> bool:
        success = True
        if target_channel_id:
            self.target_channel_id = target_channel_id
            self.target_channel = self.client.get_channel(target_channel_id)
            if not self.target_channel:
                self.logger.warning(f"Target channel not found: {target_channel_id} (optional for username/banbypass modes)")
            else:
                self.logger.info(f"Found target channel: '{self.target_channel.name}' ({target_channel_id})")
                permissions = self.target_channel.permissions_for(self.target_channel.guild.me)
                if not permissions.read_messages or not permissions.read_message_history:
                    self.logger.warning(f"Insufficient permissions for target channel {target_channel_id}")
        valid_complaint_channels = 0
        for ch_id in complaint_channel_ids:
            channel = self.client.get_channel(ch_id)
            if not channel:
                self.logger.warning(f"Complaint channel not found: {ch_id}")
                continue
            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.read_messages or not permissions.read_message_history:
                self.logger.warning(f"Insufficient permissions for complaint channel {ch_id}")
                continue
            self.complaint_channels[ch_id] = channel
            valid_complaint_channels += 1
            self.logger.info(f"Found complaint channel: '{channel.name}' ({ch_id})")
        if complaint_channel_ids and valid_complaint_channels == 0:
            self.logger.warning("No valid complaint channels could be set up")
        return success

    async def scan_target_channel(self, message_limit: int, filter_func) -> List[DiscordMessage]:
        messages = []
        if not self.target_channel_id:
            self.logger.error("Target channel ID is not set")
            return messages
        channel = self.client.get_channel(int(self.target_channel_id))
        if not channel:
            self.logger.error(f"Could not find channel with ID {self.target_channel_id}")
            return messages
        scan_limit = max(100, message_limit * 10)
        scanned_count = 0
        try:
            async for msg in channel.history(limit=scan_limit):
                scanned_count += 1
                try:
                    if filter_func(msg):
                        embed_links = {}
                        embed_titles = []
                        for embed in msg.embeds:
                            links = collect_unique_links_from_embed(embed)
                            embed_links.update(links)
                            if embed.title:
                                embed_titles.append(embed.title)
                        message = DiscordMessage(
                            id=str(msg.id),
                            channel_id=str(msg.channel.id),
                            author_id=str(msg.author.id),
                            author_name=str(msg.author),
                            content=msg.content,
                            embed_titles=embed_titles,
                            embed_links=embed_links,
                            guild_id=str(msg.guild.id),
                            created_at=msg.created_at
                        )
                        messages.append(message)
                        if len(messages) >= message_limit:
                            self.logger.info(
                                f"Found {len(messages)} matching messages after scanning {scanned_count} messages")
                            return messages
                except Exception as e:
                    self.logger.error(f"Error processing message {msg.id}: {e}", exc_info=True)
                    continue
        except discord.Forbidden:
            self.logger.error(f"Insufficient permissions to read channel {channel.name} ({channel.id})")
        except discord.HTTPException as e:
            self.logger.error(f"Discord API error reading channel {channel.name} ({channel.id}): {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error reading channel {channel.name} ({channel.id}): {e}", exc_info=True)
        self.logger.info(f"Found {len(messages)} matching messages after scanning {scanned_count} messages")
        return messages

    async def scan_target_channel_interval(
            self,
            start_message_id: str,
            end_message_id: str,
            filter_func
    ) -> List[DiscordMessage]:
        messages = []

        if not self.target_channel_id:
            self.logger.error("Target channel ID is not set")
            return messages

        channel = self.client.get_channel(int(self.target_channel_id))
        if not channel:
            self.logger.error(f"Could not find channel with ID {self.target_channel_id}")
            return messages

        try:
            start_id = int(start_message_id)
            end_id = int(end_message_id)
        except ValueError:
            self.logger.error(f"Invalid message IDs: start={start_message_id}, end={end_message_id}")
            return messages

        if start_id > end_id:
            start_id, end_id = end_id, start_id
            self.logger.info(f"Swapped message IDs to ensure chronological order")

        self.logger.info(f"Scanning messages from ID {start_id} to {end_id} in channel {channel.name}")

        scanned_count = 0
        found_start = False

        try:
            after_obj = discord.Object(id=start_id - 1)
            before_obj = discord.Object(id=end_id + 1)

            async for msg in channel.history(
                    after=after_obj,
                    before=before_obj,
                    limit=None,
                    oldest_first=True
            ):
                scanned_count += 1

                try:
                    if filter_func(msg):
                        embed_links = {}
                        embed_titles = []

                        for embed in msg.embeds:
                            from utils.embed_utils import collect_unique_links_from_embed
                            links = collect_unique_links_from_embed(embed)
                            embed_links.update(links)
                            if embed.title:
                                embed_titles.append(embed.title)

                        message = DiscordMessage(
                            id=str(msg.id),
                            channel_id=str(msg.channel.id),
                            author_id=str(msg.author.id),
                            author_name=str(msg.author),
                            content=msg.content,
                            embed_titles=embed_titles,
                            embed_links=embed_links,
                            guild_id=str(msg.guild.id),
                            created_at=msg.created_at
                        )
                        messages.append(message)

                except Exception as e:
                    self.logger.error(f"Error processing message {msg.id}: {e}", exc_info=True)
                    continue

                if scanned_count % 100 == 0:
                    self.logger.info(f"Processed {scanned_count} messages, found {len(messages)} matches")

        except discord.Forbidden:
            self.logger.error(f"Insufficient permissions to read channel {channel.name} ({channel.id})")
        except discord.HTTPException as e:
            self.logger.error(f"Discord API error reading channel {channel.name} ({channel.id}): {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error reading channel {channel.name} ({channel.id}): {e}", exc_info=True)

        self.logger.info(
            f"Interval scan complete: scanned {scanned_count} messages, "
            f"found {len(messages)} matching messages"
        )

        return messages

    async def update_complaint_cache(self, complaint_channels: Dict[int, ComplaintChannel],
                                     history_limit: int,
                                     progress_callback=None) -> Dict[int, ComplaintChannel]:
        self.logger.info("Updating complaint message cache for all complaint channels...")
        updated_channels = complaint_channels.copy()
        channel_items = list(self.complaint_channels.items())
        for ch_idx, (ch_id, discord_channel) in enumerate(channel_items):
            self.logger.info(f"Processing channel: {discord_channel.name} ({ch_id})")
            channel_cache = updated_channels.get(ch_id, ComplaintChannel(
                id=str(ch_id),
                name=discord_channel.name,
                guild_id=str(discord_channel.guild.id),
                messages=[]
            ))
            cached_message_ids = {msg.id for msg in channel_cache.messages}
            last_cached_id = channel_cache.last_cached_id
            new_messages = []
            try:
                if last_cached_id and channel_cache.messages:
                    self.logger.info(f"Fetching messages after ID {last_cached_id} in {discord_channel.name}")
                    newer_messages = await self._fetch_messages_with_retries(
                        discord_channel,
                        after=discord.Object(id=int(last_cached_id)),
                        oldest_first=True,
                        limit=None
                    )
                    for msg in newer_messages:
                        if str(msg.id) not in cached_message_ids:
                            complaint_msg = self._create_complaint_message(msg, discord_channel)
                            new_messages.append(complaint_msg)
                    self.logger.info(f"Fetched {len(new_messages)} new messages from {discord_channel.name}")
                else:
                    self.logger.info(
                        f"No last message ID found, fetching last {history_limit} messages from {discord_channel.name}")
                    all_messages = []
                    chunk_size = 1000
                    remaining = history_limit
                    before_id = None
                    total_fetched = 0
                    while remaining > 0:
                        current_chunk = min(chunk_size, remaining)
                        history_kwargs = {
                            "limit": current_chunk,
                            "oldest_first": False
                        }
                        if before_id:
                            history_kwargs["before"] = discord.Object(id=before_id)
                        chunk_messages = await self._fetch_messages_with_retries(
                            discord_channel,
                            **history_kwargs
                        )
                        if not chunk_messages:
                            break
                        all_messages.extend(chunk_messages)
                        total_fetched += len(chunk_messages)
                        remaining -= len(chunk_messages)
                        self.logger.info(f"  {discord_channel.name}: fetched {total_fetched}/{history_limit} ({total_fetched / history_limit * 100:.0f}%)...")
                        if progress_callback:
                            progress_callback(ch_idx, len(channel_items),
                                              total_fetched, history_limit,
                                              f"Загрузка {discord_channel.name}: {total_fetched}/{history_limit}")
                        if len(chunk_messages) < current_chunk:
                            break
                        before_id = int(chunk_messages[-1].id)
                    for msg in all_messages:
                        if str(msg.id) not in cached_message_ids:
                            complaint_msg = self._create_complaint_message(msg, discord_channel)
                            new_messages.append(complaint_msg)
                    self.logger.info(f"Fetched {len(new_messages)} messages from {discord_channel.name}")
                if new_messages:
                    all_messages = channel_cache.messages + new_messages
                    all_messages.sort(key=lambda x: int(x.id), reverse=True)
                    channel_cache.messages = all_messages[:history_limit]
                    if channel_cache.messages:
                        channel_cache.last_cached_id = channel_cache.messages[0].id
                    self.logger.info(
                        f"Updated cache for {discord_channel.name}: Added {len(new_messages)} messages, total: {len(channel_cache.messages)}")
                else:
                    self.logger.info(f"No new messages found in {discord_channel.name}")
            except discord.Forbidden:
                self.logger.warning(f"Insufficient permissions to read channel {discord_channel.name} ({ch_id})")
            except discord.HTTPException as e:
                self.logger.error(f"Discord API error reading channel {discord_channel.name} ({ch_id}): {e}")
            except Exception as e:
                self.logger.error(f"Unexpected error reading channel {discord_channel.name} ({ch_id}): {e}",
                                  exc_info=True)
            updated_channels[ch_id] = channel_cache
        return updated_channels

    async def _fetch_messages_with_retries(self, channel, **kwargs) -> List[discord.Message]:
        messages = []
        for attempt in range(self.max_retries):
            try:
                temp_messages = []
                async for msg in channel.history(**kwargs):
                    temp_messages.append(msg)
                messages = temp_messages
                break
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = e.retry_after if hasattr(e, 'retry_after') else self.retry_delay
                    self.logger.warning(
                        f"Rate limit hit, retrying after {retry_after}s (attempt {attempt + 1}/{self.max_retries})")
                    await asyncio.sleep(retry_after)
                else:
                    self.logger.error(f"HTTP error fetching messages (attempt {attempt + 1}/{self.max_retries}): {e}")
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(self.retry_delay)
            except Exception as e:
                self.logger.error(f"Error fetching messages (attempt {attempt + 1}/{self.max_retries}): {e}",
                                  exc_info=True)
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
        return messages

    def _create_complaint_message(self, msg: discord.Message, channel: discord.TextChannel) -> ComplaintMessage:
        try:
            embeds_data = []
            for embed in msg.embeds:
                try:
                    embed_dict = embed.to_dict()
                    filtered_embed = {
                        k: embed_dict[k] for k in ["title", "description", "fields"]
                        if k in embed_dict
                    }
                    embeds_data.append(filtered_embed)
                except Exception as e:
                    self.logger.warning(f"Error converting embed to dict: {e}")
            complaint_msg = ComplaintMessage(
                id=str(msg.id),
                content=msg.content if msg.content else "",
                embeds=embeds_data,
                channel_id=str(channel.id),
                guild_id=str(channel.guild.id)
            )
            if hasattr(msg, 'author'):
                setattr(complaint_msg, 'author', {
                    'name': str(msg.author),
                    'id': str(msg.author.id)
                })
            return complaint_msg
        except Exception as e:
            self.logger.error(f"Error creating complaint message: {e}", exc_info=True)
            return ComplaintMessage(
                id=str(msg.id),
                content="",
                embeds=[],
                channel_id=str(channel.id),
                guild_id=str(channel.guild.id)
            )

    async def find_nickname_mentions(
            self,
            nicknames: List[str],
            complaint_channels: Dict[int, ComplaintChannel],
            search_term: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if not nicknames or not complaint_channels:
            return []
        valid_nicknames = []
        lowercase_nicknames = set()
        for nick in nicknames:
            if nick and isinstance(nick, str) and len(nick) >= 2:
                valid_nicknames.append(nick)
                lowercase_nicknames.add(nick.lower())
        if not valid_nicknames:
            self.logger.warning("No valid nicknames provided for searching")
            return []
        combined_pattern = None
        try:
            patterns = []
            for nickname in valid_nicknames:
                patterns.append(f"\\b{re.escape(nickname)}\\b")
            combined_pattern = re.compile("|".join(patterns), re.IGNORECASE)
        except Exception as e:
            self.logger.warning(f"Error creating combined regex: {e}")
        admin_patterns = [re.compile(rf"администратор[^а-я]*{re.escape(nick)}\b", re.IGNORECASE) for nick in valid_nicknames]
        search_term_lower = search_term.lower() if search_term else None
        result = []
        concurrent_tasks = []
        processed_message_ids = set()
        total_messages = sum(len(channel_data.messages) for channel_data in complaint_channels.values())
        self.logger.info(
            f"Search for {len(valid_nicknames)} nicknames across {total_messages} messages in {len(complaint_channels)} channels")

        async def process_channel(ch_id, ch_data):
            nonlocal processed_message_ids
            channel_results = []
            if not ch_data.messages:
                return channel_results
            guild_id = ch_data.guild_id
            if not guild_id or guild_id == "0":
                discord_channel = self.client.get_channel(int(ch_id))
                if discord_channel and hasattr(discord_channel, 'guild'):
                    guild_id = str(discord_channel.guild.id)
                    ch_data.guild_id = guild_id
            batch_size = 200
            messages_to_process = []
            if search_term_lower:
                for msg in ch_data.messages:
                    if msg.id in processed_message_ids:
                        continue
                    processed_message_ids.add(msg.id)
                    msg_content = msg.content.lower() if msg.content else ""
                    if search_term_lower in msg_content:
                        messages_to_process.append(msg)
                        continue
                    has_embeds = hasattr(msg, 'embeds') and msg.embeds
                    if has_embeds:
                        embeds_text = self._get_embeds_text(msg)
                        if search_term_lower in embeds_text.lower():
                            messages_to_process.append(msg)
            else:
                for msg in ch_data.messages:
                    if msg.id not in processed_message_ids:
                        processed_message_ids.add(msg.id)
                        messages_to_process.append(msg)
            for i in range(0, len(messages_to_process), batch_size):
                batch = messages_to_process[i:i + batch_size]
                for message in batch:
                    try:
                        content_lower = message.content.lower() if message.content else ""
                        found_nickname = False
                        for nick_lower in lowercase_nicknames:
                            if nick_lower in content_lower:
                                found_nickname = True
                                break
                        if not found_nickname and hasattr(message, 'embeds') and message.embeds:
                            embeds_text = self._get_embeds_text(message).lower()
                            for nick_lower in lowercase_nicknames:
                                if nick_lower in embeds_text:
                                    found_nickname = True
                                    break
                        if found_nickname:
                            searchable_full = (message.content or "") + (" " + self._get_embeds_text(message) if hasattr(message, 'embeds') and message.embeds else "")
                            if any(p.search(searchable_full) for p in admin_patterns):
                                continue
                            searchable_text = content_lower
                            if hasattr(message, 'embeds') and message.embeds:
                                searchable_text += " " + self._get_embeds_text(message).lower()
                            mentioned_nicknames = []
                            if combined_pattern:
                                matches = combined_pattern.findall(searchable_text)
                                if matches:
                                    for match in matches:
                                        for nick in valid_nicknames:
                                            if nick.lower() == match.lower():
                                                if nick not in mentioned_nicknames:
                                                    mentioned_nicknames.append(nick)
                            else:
                                for nickname in valid_nicknames:
                                    if nickname.lower() in searchable_text:
                                        nick_lower = nickname.lower()
                                        index = searchable_text.find(nick_lower)
                                        while index != -1:
                                            left_ok = index == 0 or not searchable_text[index - 1].isalnum()
                                            right_ok = index + len(nick_lower) >= len(searchable_text) or not \
                                            searchable_text[index + len(nick_lower)].isalnum()
                                            if left_ok and right_ok:
                                                mentioned_nicknames.append(nickname)
                                                break
                                            index = searchable_text.find(nick_lower, index + 1)
                            if mentioned_nicknames:
                                author_name = "Unknown"
                                if hasattr(message, 'author'):
                                    if isinstance(message.author, dict):
                                        author_name = message.author.get('name', "Unknown")
                                    else:
                                        author_name = str(message.author)
                                full_text = message.content or ""
                                if hasattr(message, 'embeds') and message.embeds:
                                    full_text += " " + self._get_embeds_text(message)
                                channel_results.append({
                                    "link": f"https://discord.com/channels/{guild_id}/{ch_id}/{message.id}",
                                    "channel": ch_data.name,
                                    "content": full_text[:2000],
                                    "message_id": message.id,
                                    "message_id_as_timestamp": int(message.id),
                                    "author": author_name,
                                    "mentioned_nicknames": mentioned_nicknames
                                })
                    except Exception as e:
                        self.logger.warning(f"Error processing message {message.id}: {e}")
                        continue
            return channel_results

        max_concurrent = min(10, len(complaint_channels))
        channel_items = list(complaint_channels.items())
        for i in range(0, len(channel_items), max_concurrent):
            batch = channel_items[i:i + max_concurrent]
            batch_tasks = [process_channel(ch_id, ch_data) for ch_id, ch_data in batch]
            batch_results = await asyncio.gather(*batch_tasks)
            for channel_result in batch_results:
                result.extend(channel_result)
            processed_channels = min(i + max_concurrent, len(channel_items))
            self.logger.info(
                f"Processed {processed_channels}/{len(channel_items)} channels, found {len(result)} results so far")
        result.sort(key=lambda x: x.get("message_id_as_timestamp", 0), reverse=True)
        if search_term:
            self.logger.info(f"Found {len(result)} complaints containing '{search_term}' that mention the player(s)")
        else:
            self.logger.info(f"Found {len(result)} complaints mentioning the player(s)")
        return result

    def _get_embeds_text(self, message) -> str:
        if not hasattr(message, 'embeds') or not message.embeds:
            return ""
        text_parts = []
        for embed in message.embeds:
            if isinstance(embed, dict):
                if 'description' in embed and embed['description']:
                    text_parts.append(str(embed['description']))
                if 'title' in embed and embed['title']:
                    text_parts.append(str(embed['title']))
                if 'fields' in embed and isinstance(embed['fields'], list):
                    for field in embed['fields']:
                        if isinstance(field, dict):
                            if 'name' in field and field['name']:
                                text_parts.append(str(field['name']))
                            if 'value' in field and field['value']:
                                text_parts.append(str(field['value']))
        return " ".join(text_parts)

    def _get_searchable_text(self, message) -> str:
        searchable_text = message.content or ""
        if hasattr(message, 'embeds') and message.embeds:
            for embed in message.embeds:
                if isinstance(embed, dict):
                    if 'description' in embed and embed['description']:
                        searchable_text += " " + str(embed['description'])
                    if 'fields' in embed and isinstance(embed['fields'], list):
                        for field in embed['fields']:
                            if isinstance(field, dict):
                                if 'name' in field and field['name']:
                                    searchable_text += " " + str(field['name'])
                                if 'value' in field and field['value']:
                                    searchable_text += " " + str(field['value'])
                    if 'title' in embed and embed['title']:
                        searchable_text += " " + str(embed['title'])
        return searchable_text
