import json
import logging
import os
import tempfile
import time
from typing import Dict

from models.complaint import ComplaintChannel, ComplaintMessage

COMPLAINT_CACHE_FILENAME = "complaint_message_cache.json"
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


class CacheService:
    def __init__(self, cache_filename: str = COMPLAINT_CACHE_FILENAME) -> None:
        self.cache_filename = cache_filename
        self.logger = logging.getLogger(__name__)

    def load_complaint_cache(self) -> Dict[int, ComplaintChannel]:
        self.logger.info(f"Loading complaint message cache from {self.cache_filename}...")
        complaint_channels: Dict[int, ComplaintChannel] = {}

        if not os.path.exists(self.cache_filename):
            self.logger.info("Complaint message cache file not found.")
            return complaint_channels

        for retry in range(MAX_RETRIES):
            try:
                with open(self.cache_filename, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)

                for ch_str_id, ch_data in raw_data.items():
                    try:
                        ch_id = int(ch_str_id)
                        messages = []

                        for msg in ch_data.get("messages", []):
                            try:
                                complaint_msg = ComplaintMessage(
                                    id=msg["id"],
                                    content=msg.get("content", ""),
                                    embeds=msg.get("embeds", []),
                                    channel_id=ch_str_id,
                                    guild_id=ch_data.get("guild_id", "0")
                                )
                                messages.append(complaint_msg)
                            except Exception as e:
                                self.logger.warning(f"Error loading message from cache: {e}. Skipping message.")
                                continue

                        complaint_channels[ch_id] = ComplaintChannel(
                            id=ch_str_id,
                            name=ch_data.get("name", f"Channel {ch_str_id}"),
                            guild_id=ch_data.get("guild_id", ""),
                            messages=messages,
                            last_cached_id=ch_data.get("last_cached_id")
                        )
                    except (ValueError, TypeError, KeyError) as e:
                        self.logger.warning(
                            f"Error loading cache for channel {ch_str_id}: {e}. Skipping channel cache.")

                self.logger.info(f"Loaded cache for {len(complaint_channels)} channel(s).")
                return complaint_channels

            except json.JSONDecodeError as e:
                self.logger.error(f"JSON decode error loading complaint cache: {e}. Cache file might be corrupted.")

                backup_file = f"{self.cache_filename}.bak"
                if os.path.exists(backup_file):
                    self.logger.info(f"Attempting to restore from backup file: {backup_file}")
                    try:
                        with open(backup_file, "r", encoding="utf-8") as f:
                            raw_data = json.load(f)
                        with open(self.cache_filename, "w", encoding="utf-8") as f:
                            json.dump(raw_data, f, ensure_ascii=False, indent=4)
                        self.logger.info(f"Successfully restored cache from backup file.")
                        continue
                    except Exception as e:
                        self.logger.error(f"Failed to restore from backup: {e}")

            except Exception as e:
                self.logger.error(f"Error loading complaint cache (attempt {retry + 1}/{MAX_RETRIES}): {e}",
                                  exc_info=True)

            if retry < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

        self.logger.warning(f"Failed to load complaint cache after {MAX_RETRIES} attempts. Starting with empty cache.")
        return complaint_channels

    def save_complaint_cache(self, complaint_channels: Dict[int, ComplaintChannel]) -> bool:
        self.logger.info(f"Saving complaint message cache to {self.cache_filename}...")

        if not complaint_channels:
            self.logger.warning("No complaint channels to save.")
            return False

        cache_data = {}
        for ch_id, channel in complaint_channels.items():
            cache_data[str(ch_id)] = {
                "name": channel.name,
                "guild_id": channel.guild_id,
                "messages": [
                    {
                        "id": msg.id,
                        "content": msg.content,
                        "embeds": msg.embeds
                    }
                    for msg in channel.messages
                ],
                "last_cached_id": channel.last_cached_id
            }

        for retry in range(MAX_RETRIES):
            try:
                if os.path.exists(self.cache_filename):
                    backup_file = f"{self.cache_filename}.bak"
                    try:
                        with open(self.cache_filename, 'r', encoding='utf-8') as src:
                            with open(backup_file, 'w', encoding='utf-8') as dst:
                                dst.write(src.read())
                        self.logger.debug(f"Created backup file: {backup_file}")
                    except Exception as e:
                        self.logger.warning(f"Failed to create backup file: {e}")

                fd, temp_path = tempfile.mkstemp()
                try:
                    self.logger.debug(f"Writing to temporary file: {temp_path}")
                    with os.fdopen(fd, 'w', encoding='utf-8') as temp_file:
                        json.dump(cache_data, temp_file, ensure_ascii=False, indent=4)

                    if os.name == 'nt' and os.path.exists(self.cache_filename):
                        try:
                            os.remove(self.cache_filename)
                        except Exception as e:
                            self.logger.error(f"Failed to remove existing cache file: {e}")
                            os.unlink(temp_path)
                            if retry < MAX_RETRIES - 1:
                                time.sleep(RETRY_DELAY)
                                continue
                            else:
                                return False

                    with open(temp_path, 'r', encoding='utf-8') as src:
                        with open(self.cache_filename, 'w', encoding='utf-8') as dst:
                            dst.write(src.read())

                    os.unlink(temp_path)

                    self.logger.info(
                        f"Complaint message cache saved successfully to '{self.cache_filename}' ({len(complaint_channels)} channels with {sum(len(ch.messages) for ch in complaint_channels.values())} messages).")
                    return True

                except Exception as e:
                    self.logger.error(f"Error saving cache (attempt {retry + 1}/{MAX_RETRIES}): {e}", exc_info=True)
                    if os.path.exists(temp_path):
                        try:
                            os.unlink(temp_path)
                        except:
                            pass
            except Exception as e:
                self.logger.error(f"Unexpected error during cache save (attempt {retry + 1}/{MAX_RETRIES}): {e}",
                                  exc_info=True)

            if retry < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

        self.logger.error(f"Failed to save complaint cache after {MAX_RETRIES} attempts.")
        return False
