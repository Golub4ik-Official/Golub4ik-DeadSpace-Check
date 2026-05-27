# DeadSpace14 Ban Evasion Detector

Инструмент для автоматизации Discord, который связывает телеметрию Discord с данными панели администратора DeadSpace14 для выявления вероятных обходов бана в near real time. Проект ориентирован на опытных администраторов серверов, которым нужны воспроизводимые цепочки доказательств и автоматическая триаж подозрительных аккаунтов.

## Возможности

- Многослойная корреляция по HWID, IP, временным меткам и предыдущим банам с настраиваемыми порогами уверенности.
- Автоматический парсинг Discord — события "Arrived new player" и каналы жалоб для построения графа аккаунтов.
- Структурированный вывод: JSON (`reports/scan_report.json`) и консольные логи для интеграции с внешними инструментами.
- Кэширование, контроль конкурентности и троттлинг, оптимизированные для high-volume сообществ.
- Адаптивный оптимизатор нагрузки с circuit breaker, exponential backoff и emergency mode.
- Поиск по интервалу сообщений, массовая проверка ban bypass и исследование отдельных пользователей.

## Быстрый старт

```bash
git clone https://github.com/yourusername/deadspace14-ban-detector.git
cd deadspace14-ban-detector
python -m venv .venv
.venv\Scripts\activate  # PowerShell
pip install -r requirements.txt
copy config.py config_local.py  # опционально
python main.py
```

## Конфигурация

Основные настройки в `config.py`. Чувствительные данные переопределите перед первым запуском.

### Discord

| Ключ | Назначение |
| --- | --- |
| `DISCORD_USER_TOKEN` | Токен Discord для сканирования сообщений |
| `TARGET_CHANNEL_ID` | ID канала с событиями новых игроков |
| `COMPLAINT_CHANNEL_IDS` | ID каналов для поиска жалоб по никам |
| `MESSAGE_HISTORY_LIMIT` | Лимит истории сообщений для каналов жалоб |

### Авторизация

| Ключ | Назначение |
| --- | --- |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Учётные данные панели администратора DeadSpace14 |

### API

| Ключ | Назначение |
| --- | --- |
| `BASE_ADMIN_URL` | URL панели администратора |
| `ACCOUNT_URL` | URL SSO (account.spacestation14.com) |
| `MAX_CONCURRENT_REQUESTS` | Максимум параллельных HTTP-вызовов |
| `OPERATION_TIMEOUT` / `REQUEST_TIMEOUT` / `SEARCH_TIMEOUT` | Таймауты операций |

### Сканирование

| Ключ | Назначение |
| --- | --- |
| `MESSAGE_LIMIT` | Количество сообщений для сканирования |
| `CHECK_BAN_BYPASS` / `BAN_BYPASS_PAGES` | Режим проверки ban bypass |
| `MAX_TERMS_PER_SCAN` | Максимум терминов за одно сканирование |
| `SEARCH_MAX_DEPTH` / `SEARCH_LIMIT_*` | Глубина и лимиты рекурсивного поиска |

### Тайминги и уверенность

Пороги `CLOSE_TIME_THRESHOLD_MINUTES`, `TIME_THRESHOLD_MINUTES`, `SUSPICIOUS_TIME_THRESHOLD_MINUTES` и `IP_MATCH_TIMEDELTA_MINUTES` управляют чувствительностью детекции.

Уровни уверенности: `HWID_MATCH`, `IP_VERY_CLOSE_TIME`, `IP_CLOSE_TIME`, `IP_MODERATE_TIME`, `IP_DISTANT_TIME`, `IP_MATCH`, `NO_MATCH`.

## Режимы запуска

```bash
python main.py                                    # Базовое сканирование сообщений
python main.py --check-ban-bypass --ban-bypass-pages 10  # Проверка ban bypass
python main.py --username <ник>                   # Исследование конкретного игрока
```

Все результаты сохраняются в `reports/` и кэшируются в `complaint_message_cache.json`.

## Архитектура

```
admin_panel.py              Скрапинг панели администратора DeadSpace14 (async, aiohttp, selectolax)
bot.py                      Координационный слой Discord (discord.py-self)
core/scanner.py             Загрузка сообщений, постановка задач в очередь, circuit breaker, backoff
core/analyzer.py            Корреляция и слияние игроков по никнеймам
services/admin_service.py   Клиент API администратора, кэширование, оптимизатор нагрузки
services/cache_service.py   Персистентное кэширование жалоб (JSON)
services/discord_service.py Работа с Discord, поиск по каналам жалоб
services/reporting/         Генерация отчётов (консоль + JSON), форматирование
models/                     Типизированные модели (Player, DiscordMessage, ScanResult, Verdict, Complaint)
utils/                      Вспомогательные модули (async, logging, performance, URLs, embeds)
```

Код асинхронный (asyncio), использует пакетную обработку запросов с per-service rate guards. `services/reporting/` отвечает за форматирование JSON и консольный вывод отчётов.

## Эксплуатационные заметки

- Соблюдайте лимиты Discord и DeadSpace14; при троттлинге уменьшите `MAX_CONCURRENT_REQUESTS` или увеличьте `REQUEST_TIMEOUT`.
- Храните секреты вне репозитория (`config_local.py`, переменные окружения, секрет-менеджеры).
- Убедитесь в правах Discord на чтение целевых каналов перед запуском.
- Конфигурация может загружаться из `.json`, `.yaml` или `.py` файлов (см. `config_system.py`).

## Лицензия

MIT. Использование ограничено легитимными сценариями модерации и безопасности. Автор не поддерживает harassment, нарушение приватности или нарушение правил платформ.
