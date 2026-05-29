import logging
from typing import Dict

from models.complaint import ComplaintChannel
from services.database_service import DatabaseService


class CacheService:
    def __init__(self, db_service: DatabaseService) -> None:
        self.db = db_service
        self.logger = logging.getLogger(__name__)

    def load_complaint_cache(self) -> Dict[int, ComplaintChannel]:
        self.logger.info("Loading complaint message cache from SQLite...")
        channels = self.db.load_complaint_channels()
        if not channels:
            self.logger.warning(
                "Данные о наказаниях ещё не загружены. "
                "Первый запуск скачивает все сообщения из каналов жалоб — в среднем 10–15 минут. "
                "После завершения данные сохранятся в SQLite, и следующие запуски будут быстрыми. "
                "Альтернатива: скачать готовый deadspace_checker.db из раздела Releases."
            )
        return channels

    def save_complaint_cache(self, complaint_channels: Dict[int, ComplaintChannel]) -> bool:
        return self.db.save_complaint_cache(complaint_channels)
