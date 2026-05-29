import argparse
import logging
import sys

from admin_panel import AdminPanel
from bot import BanCheckerBot
from config_system import initialize, get_config
from utils.logging_utils import setup_logging

# ─── 1) MAIN-SPECIFIED VALUES (HIGHEST PRIORITY) ────────────────────────────
parser = argparse.ArgumentParser(description="Ban Checker Bot")
parser.add_argument("--username", type=str, default=None,
                    help="Scan reports for a specific player nickname")
parser.add_argument("--messages", type=int, default=None,
                    help="Number of messages to scan")
parser.add_argument("--check-ban-bypass", action="store_true", default=None,
                    help="Check ban bypass")
parser.add_argument("--ban-bypass-pages", type=int, default=None,
                    help="Ban bypass pages")
parser.add_argument("--log-level", type=str, default=None,
                    help="Logging level (DEBUG, INFO, WARNING, ERROR)")
parser.add_argument("--config", type=str, default="config.py",
                    help="Path to config file")
parser.add_argument("--graph", type=str, nargs="?", const="html", default=None,
                    choices=["html", "png"],
                    help="Render connection graph (html or png)")
parser.add_argument("--graph-output", type=str, default=None,
                    help="Output path for graph file")
args = parser.parse_args()

MESSAGE_LIMIT = args.messages if args.messages is not None else 700
USERNAME = args.username
CHECK_BAN_BYPASS = bool(args.check_ban_bypass) if args.check_ban_bypass is not None else False
BAN_BYPASS_PAGES = args.ban_bypass_pages if args.ban_bypass_pages is not None else 1

MESSAGE_INTERVAL_START = None
MESSAGE_INTERVAL_END = None
# Example:
# MESSAGE_INTERVAL_START = "https://discord.com/channels/1030160796401016883/1315754807595761695/1402266517202141184"
# MESSAGE_INTERVAL_END = "https://discord.com/channels/1030160796401016883/1315754807595761695/1402287086844772383"

SEARCH_DEPTH = None
SEARCH_LIMIT_ROOT = None
SEARCH_LIMIT_LEVEL1 = None
SEARCH_LIMIT_LEVEL2 = None
SEARCH_LIMIT_DEFAULT = None

GRAPH_FORMAT = args.graph
GRAPH_OUTPUT = args.graph_output
LOG_LEVEL = args.log_level
CONFIG_FILE = args.config
# ────────────────────────────────────────────────────────────────────────────────

def main():
    try:
        initialize(CONFIG_FILE)
        cfg = get_config()
    except Exception as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    # ─── 2) OVERRIDE WITH MAIN VARIABLES (if not None) ─────────────────────────────
    if MESSAGE_LIMIT is not None:
        cfg.scan.message_limit = MESSAGE_LIMIT
    if USERNAME is not None:
        cfg.scan.username = USERNAME
    if CHECK_BAN_BYPASS is not None:
        cfg.scan.check_ban_bypass = CHECK_BAN_BYPASS
    if BAN_BYPASS_PAGES is not None:
        cfg.scan.ban_bypass_pages = BAN_BYPASS_PAGES

    if SEARCH_DEPTH is not None:
        cfg.scan.search_max_depth = SEARCH_DEPTH
    if SEARCH_LIMIT_ROOT is not None:
        cfg.scan.search_limit_root = SEARCH_LIMIT_ROOT
    if SEARCH_LIMIT_LEVEL1 is not None:
        cfg.scan.search_limit_level1 = SEARCH_LIMIT_LEVEL1
    if SEARCH_LIMIT_LEVEL2 is not None:
        cfg.scan.search_limit_level2 = SEARCH_LIMIT_LEVEL2
    if SEARCH_LIMIT_DEFAULT is not None:
        cfg.scan.search_limit_default = SEARCH_LIMIT_DEFAULT

    if GRAPH_FORMAT is not None:
        cfg.report.graph_format = GRAPH_FORMAT
    if GRAPH_OUTPUT is not None:
        cfg.report.graph_output = GRAPH_OUTPUT

    if LOG_LEVEL is not None:
        cfg.logging.log_level = LOG_LEVEL
    # ────────────────────────────────────────────────────────────────────────────────

    setup_logging(
        log_file=cfg.logging.log_file,
        level=getattr(logging, cfg.logging.log_level),
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
        use_colors=cfg.logging.use_colors,
        log_dir=cfg.logging.log_dir
    )

    logging.info("Starting Ban Checker Bot")
    if MESSAGE_INTERVAL_START and MESSAGE_INTERVAL_END:
        mode = f"Interval: from {MESSAGE_INTERVAL_START} to {MESSAGE_INTERVAL_END}"
    elif cfg.scan.check_ban_bypass:
        mode = "Ban Bypass Check"
    elif cfg.scan.username:
        mode = f"Username: {cfg.scan.username}"
    else:
        mode = f"Messages: {cfg.scan.message_limit}"

    logging.info(f"Scan mode: {mode}")

    admin_panel = AdminPanel(cfg.auth.admin_username, cfg.auth.admin_password)

    bot_config = {
        "TARGET_CHANNEL_ID": cfg.discord.target_channel_id,
        "COMPLAINT_CHANNEL_IDS": cfg.discord.complaint_channel_ids,
        "COMPLAINT_MESSAGE_HISTORY_LIMIT": cfg.discord.message_history_limit,
        "message_limit": cfg.scan.message_limit,
        "username": cfg.scan.username,
        "check_ban_bypass": cfg.scan.check_ban_bypass,
        "ban_bypass_pages": cfg.scan.ban_bypass_pages,
        "html_report_filename": cfg.report.html_report_filename,
        "graph_format": cfg.report.graph_format,
        "graph_output": cfg.report.graph_output,
        "message_interval_start": MESSAGE_INTERVAL_START,
        "message_interval_end": MESSAGE_INTERVAL_END
    }

    bot = BanCheckerBot(cfg.discord.discord_user_token, admin_panel, bot_config)
    bot.run()

if __name__ == "__main__":
    main()
