# Golub4ik (WikiHampter) DeadSpace Checker

Инструмент для администраторов SS14, который связывает телеметрию Discord с данными панели администратора DeadSpace14 для выявления обходов бана.

## Возможности

- Три режима сканирования: пробив по нику, сканирование новых сообщений, проверка обхода банов
- Многослойная корреляция по HWID, IP, временным меткам и предыдущим банам
- Автоматический парсинг Discord — события "Arrived new player" и каналы жалоб
- Генерация HTML-отчётов с детальной информацией по каждому игроку
- Кэширование, контроль конкурентности, circuit breaker и exponential backoff
- Удобный GUI на tkinter с цветным выводом и встроенными настройками

## Быстрый старт

### Вариант 1 — Готовый EXE (Python не нужен)

Скачай последнюю версию в разделе **Releases** этого репозитория — там лежит собранный `DeadSpaceChecker.exe`. Просто запусти — база данных создаётся автоматически. Папка `reports/` появится после первого сканирования.

### Вариант 2 — Из исходников (с Python)

```bash
git clone <repo>
cd Golub4ik-DeadSpace-Check
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python gui.py
```

### Вариант 3 — С готовой базой (первый запуск мгновенно)

Первый сбор кэша жалоб занимает 10–15 минут (скачиваются все сообщения из каналов Discord).
Чтобы не ждать, можно скачать готовый `deadspace_checker.db` из раздела **Releases**.

**Куда положить файл:**
- Если пользуетесь EXE — положите `deadspace_checker.db` рядом с `DeadSpaceChecker.exe`
- Если запускаете из исходников — положите в корень репозитория (туда же, где лежит `gui.py`)

Программа сама найдёт файл при запуске, ничего настраивать не нужно. Дальнейшие запуски будут докачивать только новые сообщения — это занимает секунды.

**Если готовой базы в релизе нет** — создайте её самостоятельно (потребуется Python и Discord токен):

```bash
pip install -r requirements.txt
python build_cache.py --token ВАШ_DISCORD_ТОКЕН
```

После завершения появится `deadspace_checker.db`. Его можно использовать самому или загрузить в релиз для других пользователей.

## Интерфейс

Программа запускается через графическое окно (`gui.py`). Все настройки (токен Discord, учётные данные админки, параметры сканирования) задаются прямо в интерфейсе и сохраняются в `gui_settings.json`.

- **Пробив игрока по нику** — вводишь ник, получаешь полную информацию: наказания, связанные аккаунты, IP, HWID, жалобы
- **Сканирование новых сообщений** — мониторинг канала "Arrived new player"
- **Проверка обхода банов** — массовая проверка на ban bypass

После завершения сканирования можно сформировать HTML-отчёт.

### Как получить Discord токен

1. Открой Discord (десктоп или браузер)
2. Нажми F12 (или Ctrl+Shift+I)
3. Перейди на вкладку Network
4. Отправь любое сообщение в чат
5. Найди запрос к `discord.com/api/`
6. Скопируй значение заголовка `authorization`

## Сборка EXE (для разработчиков)

```bash
pip install pyinstaller
pyinstaller DeadSpaceChecker.spec --noconfirm
```

EXE появится в `dist/DeadSpaceChecker.exe`. `config.py` вшивается внутрь — менять настройки можно через GUI, они сохранятся в `gui_settings.json` рядом с exe.

## Конфигурация

Основные настройки задаются через GUI (кнопка ⚙️). Для продвинутой конфигурации можно отредактировать `config.py` перед сборкой.

## Режимы запуска (CLI)

```bash
python main.py                                    # Базовое сканирование сообщений
python main.py --check-ban-bypass --ban-bypass-pages 10  # Проверка ban bypass
python main.py --username <ник>                   # Исследование конкретного игрока
```

## Архитектура

```
gui.py                      Графический интерфейс (tkinter)
admin_panel.py              Скрапинг панели администратора (async, aiohttp, selectolax)
bot.py                      Координационный слой Discord (discord.py-self)
build_cache.py              CLI-скрипт для предварительного создания БД кэша
core/scanner.py             Загрузка сообщений, очередь задач, circuit breaker
core/analyzer.py            Корреляция и слияние игроков по никнеймам
services/database_service.py   SQLite-бэкенд (кэш жалоб + админ-кэш + настройки)
services/cache_service.py   Обёртка вокруг database_service для ComplaintChannel
services/admin_service.py   Клиент API администратора, кэширование через SQLite
services/discord_service.py Работа с Discord, поиск по каналам
services/reporting/         Генерация отчётов (HTML + JSON)
models/                     Типизированные модели (Player, DiscordMessage, ScanResult, Verdict, Complaint)
utils/                      Вспомогательные модули (async, logging, performance, URLs, embeds)
DeadSpaceChecker.spec       Спецификация PyInstaller для сборки EXE
```

## Заметки

- Соблюдайте лимиты Discord и DeadSpace14; при троттлинге уменьшите лимиты в настройках GUI
- Храните секреты вне репозитория
- Убедитесь в правах Discord на чтение целевых каналов перед запуском

## Лицензия

MIT. Использование ограничено легитимными сценариями модерации и безопасности.
