import os
import shutil
from typing import Optional

TERMINAL_FORMATTING = {
    'HEADER': '\033[95m',
    'BLUE': '\033[94m',
    'CYAN': '\033[96m',
    'GREEN': '\033[92m',
    'YELLOW': '\033[93m',
    'RED': '\033[91m',
    'BOLD': '\033[1m',
    'UNDERLINE': '\033[4m',
    'END': '\033[0m',
    'GRAY': '\033[90m',
    'WHITE': '\033[97m',
    'ITALIC': '\033[3m',

    'BG_BLACK': '\033[40m',
    'BG_RED': '\033[41m',
    'BG_GREEN': '\033[42m',
    'BG_YELLOW': '\033[43m',
    'BG_BLUE': '\033[44m',
    'BG_MAGENTA': '\033[45m',
    'BG_CYAN': '\033[46m',
    'BG_WHITE': '\033[47m',

    'BRIGHT_BLACK': '\033[90m',
    'BRIGHT_RED': '\033[91m',
    'BRIGHT_GREEN': '\033[92m',
    'BRIGHT_YELLOW': '\033[93m',
    'BRIGHT_BLUE': '\033[94m',
    'BRIGHT_MAGENTA': '\033[95m',
    'BRIGHT_CYAN': '\033[96m',
    'BRIGHT_WHITE': '\033[97m',

    'BRIGHT_YELLOW_BOLD': '\033[93;1m',
    'WHITE_BOLD': '\033[97;1m',

    'BLUE_UNDERLINE': '\033[94;4m',

    'RED_UNDERLINE': '\033[91;4m',
    'GREEN_UNDERLINE': '\033[92;4m',
    'YELLOW_UNDERLINE': '\033[93;4m',
    'CYAN_UNDERLINE': '\033[96;4m',

    'GREEN_BOLD': '\033[92;1m',
    'YELLOW_BOLD': '\033[93;1m',
    'RED_BOLD': '\033[91;1m',
}

BOX_CHARS = {
    'H': '─',
    'V': '│',
    'TL': '┌',
    'TR': '┐',
    'BL': '└',
    'BR': '┘',
    'VL': '┤',
    'VR': '├',
    'HU': '┴',
    'HD': '┬',
    'CROSS': '┼',
    'DOUBLE_H': '═',
    'DOUBLE_V': '║',
    'DOUBLE_TL': '╔',
    'DOUBLE_TR': '╗',
    'DOUBLE_BL': '╚',
    'DOUBLE_BR': '╝',
    'DOUBLE_VL': '╣',
    'DOUBLE_VR': '╠',
    'DOUBLE_HU': '╩',
    'DOUBLE_HD': '╦',
    'DOUBLE_CROSS': '╬',
    'BULLET': '•',
    'ARROW': '→',
    'RIGHT_ARROW': '►',
    'DOWN_ARROW': '▼',
    'CHECK': '✓',
    'X_MARK': '✗',
    'WARNING': '⚠',
    'INFO': 'ℹ',
    'STAR': '★',
    'CIRCLE': '○',
    'FILLED_CIRCLE': '●',
    'SUB_ARROW': '↳',
}

ASCII_BOX_CHARS = {
    'H': '-',
    'V': '|',
    'TL': '+',
    'TR': '+',
    'BL': '+',
    'BR': '+',
    'VL': '+',
    'VR': '+',
    'HU': '+',
    'HD': '+',
    'CROSS': '+',
    'DOUBLE_H': '=',
    'DOUBLE_V': '|',
    'DOUBLE_TL': '+',
    'DOUBLE_TR': '+',
    'DOUBLE_BL': '+',
    'DOUBLE_BR': '+',
    'DOUBLE_VL': '+',
    'DOUBLE_VR': '+',
    'DOUBLE_HU': '+',
    'DOUBLE_HD': '+',
    'DOUBLE_CROSS': '+',
    'BULLET': '*',
    'ARROW': '->',
    'RIGHT_ARROW': '>',
    'DOWN_ARROW': 'v',
    'CHECK': '+',
    'X_MARK': 'x',
    'WARNING': '!',
    'INFO': 'i',
    'STAR': '*',
    'CIRCLE': 'o',
    'FILLED_CIRCLE': '#',
    'SUB_ARROW': '->',
}

DISPLAY_LIMITS = {
    'SMALL': 3,
    'MEDIUM': 5,
    'LARGE': 10,
    'XLARGE': 20,

    'COMPLAINT_LIMIT': 20,
    'NICKNAME_DISPLAY_LIMIT': 7,
    'BAN_REASON_DISPLAY_LIMIT': 5,
    'IP_OWNED_DISPLAY_LIMIT': 10,
    'IP_ALT_DISPLAY_LIMIT': 5,
    'IP_OTHER_DISPLAY_LIMIT': 3,
    'HWID_OWNED_DISPLAY_LIMIT': 10,
    'HWID_ALT_DISPLAY_LIMIT': 5,
    'HWID_OTHER_DISPLAY_LIMIT': 3,
    'LOGIN_DISPLAY_LIMIT': 5,
    'IP_RANGE_DISPLAY_LIMIT': 5,
    'CONNECTION_PATH_DISPLAY_LIMIT': 5,
    'MULTI_ALT_DISPLAY_LIMIT': 7,
    'COMPLAINT_SAMPLE_LIMIT': 3,
    'SUB-COMPLAINT_HWID_LIMIT': 20,
    'SUB-COMPLAINT_IP_LIMIT': 20,
    'SUMMARY_LIST_LIMIT': 5,
}

LAYOUT_CONFIG = {
    'COLOR_INTENSITY_THRESHOLD': 1,
    'PADDING_SMALL': 2,
    'PADDING_MEDIUM': 4,
    'PADDING_LARGE': 6,
    'CONTENT_WIDTH_REDUCTION': 12,
    'HEADER_MIN_PADDING': 1,
    'TABLE_COLUMN_MARGIN': 3,
    'INDENT_SIZE': 2,
    'SAMPLE_TEXT_SIZE': 500,
    'STAT_BOX_COLUMN_PADDING': 2,
    'DETAIL_BOX_WIDTH_REDUCTION': 8,
    'CONTENT_BOX_WIDTH_REDUCTION': 10,
    'DEFAULT_INDENT_STRING': "  ",
}

ANALYSIS_CONFIG = {
    'STRONG_CONNECTION_THRESHOLD': 1.0,
    'DIRECT_CONNECTION_STRENGTH': 2.0,
    'SINGLE_CONNECTION_STRENGTH': 1.0,
    'IP_CONNECTION_STRENGTH': 0.5,

    'PRIMARY_OWNER_WEIGHT': 3,
    'ALT_OWNER_WEIGHT': 2,
    'LOGIN_OWNER_WEIGHT': 1
}

DEFAULT_REPORT_CONFIG = {
    'BOX_WIDTH_LARGE': 120,
    'BOX_WIDTH_MEDIUM': 100,
    'BOX_WIDTH_SMALL': 80,

    'TRUNCATE_LIST_LIMIT': 14,
    'TRUNCATE_TEXT_LENGTH': 80,
    'DISPLAY_LIMIT_SMALL': 20,
    'DISPLAY_LIMIT_MEDIUM': 40,
    'DISPLAY_LIMIT_LARGE': 80,

    'DETAIL_LEVEL': 2,
    'COLOR_INTENSITY': 1,
    'SHOW_TIMESTAMPS': True,

    'COUNT_THRESHOLD_MEDIUM': 5,
    'COUNT_THRESHOLD_HIGH': 20,
}

REPORT_FILE_SETTINGS = {
    'REPORT_FILENAME': 'scan_report.json',
    'REPORT_OUTPUT_DIR': 'reports',
}

PLAYER_STATUS = {
    'BANNED': 'banned',
    'SUSPICIOUS': 'suspicious',
    'CLEAN': 'clean',
    'UNKNOWN': 'unknown',
}

SEVERITY_LEVELS = {
    'HIGH': ['HIGH', 'CRITICAL', 'STRONG'],
    'MEDIUM': ['MEDIUM', 'MODERATE'],
    'LOW': ['LOW', 'MINIMAL'],
}

CONFIDENCE_LEVELS = {
    'HIGH': ['HIGH', 'CERTAIN'],
    'MEDIUM': ['MEDIUM', 'MODERATE', 'LIKELY'],
    'LOW': ['LOW', 'UNCERTAIN', 'UNLIKELY'],
}

TIME_ANALYSIS_THRESHOLDS = {
    'RECENT_LOGIN_DAYS': 30,
    'HISTORICAL_LOGIN_DAYS': 180,
}


class ReportConfig:

    def __init__(self, **kwargs):
        terminal_size = shutil.get_terminal_size((DEFAULT_REPORT_CONFIG['BOX_WIDTH_LARGE'], 40))
        terminal_width = terminal_size.columns

        self.box_width_large = min(
            kwargs.get('box_width_large', DEFAULT_REPORT_CONFIG['BOX_WIDTH_LARGE']),
            terminal_width - LAYOUT_CONFIG['PADDING_SMALL'] 
        )
        self.box_width_medium = min(
            kwargs.get('box_width_medium', DEFAULT_REPORT_CONFIG['BOX_WIDTH_MEDIUM']),
            terminal_width - LAYOUT_CONFIG['PADDING_SMALL']
        )
        self.box_width_small = min(
            kwargs.get('box_width_small', DEFAULT_REPORT_CONFIG['BOX_WIDTH_SMALL']),
            terminal_width - LAYOUT_CONFIG['PADDING_SMALL']
        )
        self.box_width_medium = min(self.box_width_medium, self.box_width_large)
        self.box_width_small = min(self.box_width_small, self.box_width_medium)


        self.truncate_list_limit = kwargs.get(
            'truncate_list_limit',
            DEFAULT_REPORT_CONFIG['TRUNCATE_LIST_LIMIT']
        )
        self.truncate_text_length = kwargs.get(
            'truncate_text_length',
            DEFAULT_REPORT_CONFIG['TRUNCATE_TEXT_LENGTH']
        )
        
        self.display_limit_small_items = kwargs.get(
            'display_limit_small', 
            DEFAULT_REPORT_CONFIG['DISPLAY_LIMIT_SMALL']
        )
        self.display_limit_medium_items = kwargs.get(
            'display_limit_medium',
            DEFAULT_REPORT_CONFIG['DISPLAY_LIMIT_MEDIUM']
        )
        self.display_limit_large_items = kwargs.get(
            'display_limit_large',
            DEFAULT_REPORT_CONFIG['DISPLAY_LIMIT_LARGE']
        )

        self.detail_level = kwargs.get('detail_level', DEFAULT_REPORT_CONFIG['DETAIL_LEVEL'])
        self.color_intensity = kwargs.get('color_intensity', DEFAULT_REPORT_CONFIG['COLOR_INTENSITY'])
        self.show_timestamps = kwargs.get('show_timestamps', DEFAULT_REPORT_CONFIG['SHOW_TIMESTAMPS'])

        self.report_filename = kwargs.get('report_filename', REPORT_FILE_SETTINGS['REPORT_FILENAME'])
        self.report_output_dir = kwargs.get('report_output_dir', REPORT_FILE_SETTINGS['REPORT_OUTPUT_DIR'])
        
        self.count_threshold_medium = kwargs.get('count_threshold_medium', DEFAULT_REPORT_CONFIG['COUNT_THRESHOLD_MEDIUM'])
        self.count_threshold_high = kwargs.get('count_threshold_high', DEFAULT_REPORT_CONFIG['COUNT_THRESHOLD_HIGH'])


        os.makedirs(self.report_output_dir, exist_ok=True)

    def get_dynamic_limit(self, category: Optional[str] = None) -> int:

        if category and category.upper() in DISPLAY_LIMITS:
            base_limit = DISPLAY_LIMITS[category.upper()]
            if self.detail_level == 0:
                return max(1, round(base_limit * 0.5))
            elif self.detail_level == 1:
                return base_limit
            else:
                return round(base_limit * 1.5) if base_limit > 2 else base_limit + 1
        
        if self.detail_level == 0:
            return self.display_limit_small_items
        elif self.detail_level == 1:
            return self.display_limit_medium_items
        else:
            return self.display_limit_large_items

    def get_specific_display_limit(self, name: str) -> int:
        upper_name = name.upper()
        if upper_name not in DISPLAY_LIMITS:
            return self.get_dynamic_limit('MEDIUM') 
            
        base_limit = DISPLAY_LIMITS[upper_name]
        
        if self.detail_level == 0:
            if base_limit <= 3: return max(1, base_limit -1)
            return max(1, round(base_limit * 0.5))
        elif self.detail_level == 1:
            return base_limit
        else:
            if base_limit <=3 : return base_limit + 1
            return round(base_limit * 1.5)


def load_config_from_file(config_file: Optional[str] = None) -> dict:
    import json

    config = DEFAULT_REPORT_CONFIG.copy()

    if config_file and os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                user_config = json.load(f)
                config.update(user_config)
        except Exception as e:
            print(f"{TERMINAL_FORMATTING.get('RED', '')}Error loading config file '{config_file}': {e}{TERMINAL_FORMATTING.get('END', '')}")
    return config