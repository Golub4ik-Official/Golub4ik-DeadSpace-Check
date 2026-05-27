import logging
from typing import List, Dict, Any

import discord

from core.analyzer import PlayerAnalyzer
from core.scanner import Scanner
from services.admin_service import AdminService
from services.cache_service import CacheService
from services.discord_service import DiscordService
from services.reporting import ReportService


class BanCheckerBot:
    def __init__(self, token: str, admin_panel, config: Dict[str, Any], progress_queue=None) -> None:
        self.token = token
        self.config = config
        self.client = discord.Client()
        self.discord_service = DiscordService(self.client)
        self.admin_service = AdminService(admin_panel)
        self.cache_service = CacheService()
        self.report_service = ReportService()
        self.player_analyzer = PlayerAnalyzer()
        self.scanner = Scanner(
            self.discord_service,
            self.admin_service,
            self.cache_service,
            self.report_service,
            self.player_analyzer,
            progress_queue=progress_queue
        )
        self.client.event(self.on_ready)

    async def on_ready(self):
        logging.info(f"Logged in as: {self.client.user} (ID: {self.client.user.id})")
        target_channel_id = self.config.get("TARGET_CHANNEL_ID")
        complaint_channel_ids = self.config.get("COMPLAINT_CHANNEL_IDS", [])

        if not await self.scanner.setup(target_channel_id, complaint_channel_ids):
            logging.error("Failed to set up scanner. Exiting.")
            await self.close()
            return

        try:
            report_data: List[Dict[str, Any]] = []

            message_interval_start = self.config.get("message_interval_start")
            message_interval_end = self.config.get("message_interval_end")

            if message_interval_start and message_interval_end:
                logging.info(f"Starting interval scan from {message_interval_start} to {message_interval_end}")
                report_data = await self.scanner.scan_message_interval(
                    message_interval_start,
                    message_interval_end
                )
            elif self.config.get("check_ban_bypass"):
                logging.info("Starting ban bypass check")
                report_data = await self.scanner.scan_ban_bypasses(
                    max_pages=self.config.get("ban_bypass_pages", 5)
                )
            elif self.config.get("username"):
                logging.info(f"Starting nickname scan for: {self.config.get('username')}")
                report_data = await self.scanner.scan_nickname(
                    self.config.get("username")
                )
            elif self.config.get("message_limit") is not None:
                logging.info(f"Starting message scan with limit: {self.config.get('message_limit')}")
                report_data = await self.scanner.scan_messages(
                    message_limit=self.config.get("message_limit", 10)
                )
            else:
                logging.warning("No scan type specified or missing parameters.")

            if report_data:
                self.report_service.write_json_report(report_data)
                logging.info(f"Report with {len(report_data)} items written to file")
        except Exception as e:
            logging.error(f"Error during scan: {e}", exc_info=True)

        logging.info("Scan complete. Disconnecting from Discord.")
        await self.close()

    async def close(self):
        if hasattr(self, 'admin_service') and self.admin_service:
            try:
                await self.admin_service.close()
                logging.info("AdminService closed successfully.")
            except Exception as e:
                logging.error(f"Error closing AdminService: {e}", exc_info=True)

        if self.client:
            try:
                await self.client.close()
                logging.info("Discord client closed successfully.")
            except Exception as e:
                logging.error(f"Error closing Discord client: {e}", exc_info=True)

    def run(self):
        self.client.run(self.token)
