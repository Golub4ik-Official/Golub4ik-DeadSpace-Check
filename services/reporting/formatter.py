import sys
import textwrap
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from services.reporting.config import (
    ReportConfig, TERMINAL_FORMATTING, BOX_CHARS, ASCII_BOX_CHARS,
    SEVERITY_LEVELS, CONFIDENCE_LEVELS, DEFAULT_REPORT_CONFIG,
    LAYOUT_CONFIG
)


class ReportFormatter:

    def __init__(self, config: Optional[ReportConfig] = None):
        self.config = config or ReportConfig()
        self.fmt = self._setup_terminal_formatting()
        self.box = self._get_box_chars()
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _get_box_chars():
        try:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            encoding = sys.stdout.encoding or 'utf-8'
            test_char = BOX_CHARS['H']
            test_char.encode(encoding, errors='strict')
            return BOX_CHARS.copy()
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            return ASCII_BOX_CHARS.copy()

    def _get_fmt(self, main_key: str, *modifier_keys: str, default: str = '') -> str:
        main_key_upper = main_key.upper()
        mod_keys_upper = [mk.upper() for mk in modifier_keys]

        if mod_keys_upper:
            combined_key = f"{main_key_upper}_{'_'.join(mod_keys_upper)}"
            if combined_key in self.fmt:
                return self.fmt[combined_key]

        base_color = self.fmt.get(main_key_upper, '')
        modifiers_str = "".join(self.fmt.get(mod_key, '') for mod_key in mod_keys_upper)
        
        if base_color or modifiers_str:
            return base_color + modifiers_str
        return default

    def _setup_terminal_formatting(self) -> Dict[str, str]:
        if sys.stdout.isatty():
            base_fmt = TERMINAL_FORMATTING.copy()
            if self.config.color_intensity >= LAYOUT_CONFIG['COLOR_INTENSITY_THRESHOLD']:
                color_keys_to_combine = [
                    'HEADER', 'BLUE', 'CYAN', 'GREEN', 'YELLOW', 'RED', 'GRAY', 'WHITE',
                    'BRIGHT_BLACK', 'BRIGHT_RED', 'BRIGHT_GREEN', 'BRIGHT_YELLOW',
                    'BRIGHT_BLUE', 'BRIGHT_MAGENTA', 'BRIGHT_CYAN', 'BRIGHT_WHITE'
                ]
                style_modifiers = {
                    'BOLD': base_fmt.get('BOLD', ''),
                    'UNDERLINE': base_fmt.get('UNDERLINE', ''),
                    'ITALIC': base_fmt.get('ITALIC', '')
                }
                for color_key in color_keys_to_combine:
                    if color_key in base_fmt:
                        for style_name, style_code in style_modifiers.items():
                            if style_code:
                                if base_fmt[color_key]:
                                    base_fmt[f'{color_key}_{style_name}'] = base_fmt[color_key] + style_code
                                elif style_name == color_key:
                                     base_fmt[f'{color_key}_{style_name}'] = style_code
            return base_fmt
        else:
            return {key: '' for key in TERMINAL_FORMATTING}

    def print_header(self, title: str, width: Optional[int] = None, style: str = 'header'):
        width = width or self.config.box_width_large
        self._print_boxed(title, width, style=style)

        if self.config.show_timestamps:
            timestamp_str = f"Report generated: {self.timestamp}"
            print(f"{self._get_fmt('GRAY')}{timestamp_str:>{width}}{self.fmt['END']}")


    def print_section(self, title: str, width: Optional[int] = None, style: str = 'section'):
        width = width or self.config.box_width_medium
        self._print_boxed(title, width, style=style)

    def _print_boxed(self, title: str, width: int, style: str = 'default'):
        fmt = self.fmt
        box = self.box

        style_definitions = {
            'default':    {'color_keys': ('BOLD',), 'chars': ('TL', 'TR', 'BL', 'BR', 'H', 'V'), 'prefix': '', 'suffix': ''},
            'header':     {'color_keys': ('HEADER', 'BOLD'), 'chars': ('DOUBLE_TL', 'DOUBLE_TR', 'DOUBLE_BL', 'DOUBLE_BR', 'DOUBLE_H', 'DOUBLE_V'), 'prefix': '', 'suffix': ''},
            'section':    {'color_keys': ('BRIGHT_CYAN', 'BOLD'), 'chars': ('TL', 'TR', 'BL', 'BR', 'H', 'V'), 'prefix': '', 'suffix': ''},
            'subsection': {'color_keys': ('CYAN', 'BOLD'), 'chars': ('TL', 'TR', 'BL', 'BR', 'H', 'V'), 'prefix': '', 'suffix': ''},
            'warning':    {'color_keys': ('RED', 'BOLD'), 'chars': ('TL', 'TR', 'BL', 'BR', 'H', 'V'), 'prefix': f"{box['WARNING']} ", 'suffix': f" {box['WARNING']}"},
            'success':    {'color_keys': ('GREEN', 'BOLD'), 'chars': ('TL', 'TR', 'BL', 'BR', 'H', 'V'), 'prefix': f"{box['CHECK']} ", 'suffix': f" {box['CHECK']}"},
        }

        attrs = style_definitions.get(style, style_definitions['default'])
        
        color_prefix = self._get_fmt(*attrs['color_keys'])
        bc_keys = ('TL', 'TR', 'BL', 'BR', 'H', 'V')
        current_box_chars = {key: box[val_key] for key, val_key in zip(bc_keys, attrs['chars'])}
        
        effective_title_colored = attrs['prefix'] + title + attrs['suffix']
        
        h_bar_len = width - 2 
        if h_bar_len < 0: h_bar_len = 0

        print(f"\n{color_prefix}{current_box_chars['TL']}{current_box_chars['H'] * h_bar_len}{current_box_chars['TR']}{fmt['END']}")

        space_for_title_and_padding = width - 2 
        min_side_padding = LAYOUT_CONFIG['HEADER_MIN_PADDING']
        
        plain_effective_title_for_len = attrs['prefix'] + title + attrs['suffix']
        
        max_title_len_plain = space_for_title_and_padding - (min_side_padding * 2)

        title_to_display_formatted = effective_title_colored
        if len(plain_effective_title_for_len) > max_title_len_plain and max_title_len_plain > 3:
            title_text_part = title 
            available_for_title_text = max_title_len_plain - (len(attrs['prefix']) + len(attrs['suffix']))
            if available_for_title_text > 3 :
                title_text_part_truncated = title_text_part[:available_for_title_text-3] + "..."
            else:
                title_text_part_truncated = title_text_part[:available_for_title_text]
            title_to_display_formatted = attrs['prefix'] + title_text_part_truncated + attrs['suffix']
            plain_effective_title_for_len = attrs['prefix'] + title_text_part_truncated + attrs['suffix']

        elif len(plain_effective_title_for_len) > max_title_len_plain:
             title_to_display_formatted = plain_effective_title_for_len[:max_title_len_plain]
             plain_effective_title_for_len = title_to_display_formatted


        centering_padding_total = space_for_title_and_padding - len(plain_effective_title_for_len)
        if centering_padding_total < 0: centering_padding_total = 0
            
        left_centering_pad = centering_padding_total // 2
        right_centering_pad = centering_padding_total - left_centering_pad
        
        print(
            f"{color_prefix}{current_box_chars['V']}{fmt['END']}"
            f"{' ' * left_centering_pad}{title_to_display_formatted}{' ' * right_centering_pad}"
            f"{color_prefix}{current_box_chars['V']}{fmt['END']}")

        print(f"{color_prefix}{current_box_chars['BL']}{current_box_chars['H'] * h_bar_len}{current_box_chars['BR']}{fmt['END']}")

    def print_player_header(self, name: str, indent_str: str = LAYOUT_CONFIG['DEFAULT_INDENT_STRING'], width: Optional[int] = None):
        width = width or self.config.box_width_medium
        box = self.box
        fmt = self.fmt
        color = self._get_fmt('BRIGHT_YELLOW', 'BOLD')

        player_header_text = f"PLAYER: {name}"
        h_bar_len = width - 2 
        if h_bar_len < 0: h_bar_len = 0
        
        print(f"\n{indent_str}{color}{box['DOUBLE_TL']}{box['DOUBLE_H'] * h_bar_len}{box['DOUBLE_TR']}{fmt['END']}")
        
        space_for_title_and_padding = width - 2
        
        centering_padding_total = space_for_title_and_padding - len(player_header_text)
        if centering_padding_total < 0: centering_padding_total = 0
            
        left_pad_len = centering_padding_total // 2
        right_pad_len = centering_padding_total - left_pad_len

        print(
            f"{indent_str}{color}{box['DOUBLE_V']}{' ' * left_pad_len}"
            f"{player_header_text}{' ' * right_pad_len}{box['DOUBLE_V']}{fmt['END']}")
        print(f"{indent_str}{color}{box['DOUBLE_VR']}{box['DOUBLE_H'] * h_bar_len}{box['DOUBLE_VL']}{fmt['END']}")


    def print_section_header(self, title: str, indent_str: str = LAYOUT_CONFIG['DEFAULT_INDENT_STRING'], width: Optional[int] = None, style: str = 'normal'):
        width = width or self.config.box_width_medium
        box = self.box
        fmt = self.fmt
        outer_color = self._get_fmt('BOLD')

        style_map = {
            'warning': {'color_keys': ('RED', 'BOLD'), 'icon': box['WARNING']},
            'success': {'color_keys': ('GREEN', 'BOLD'), 'icon': box['CHECK']},
            'info': {'color_keys': ('BLUE', 'BOLD'), 'icon': box['INFO']},
            'important': {'color_keys': ('YELLOW', 'BOLD'), 'icon': box['STAR']},
            'normal': {'color_keys': ('BRIGHT_BLUE', 'BOLD'), 'icon': ''},
        }
        
        current_style = style_map.get(style, style_map['normal'])
        title_color_str = self._get_fmt(*current_style['color_keys'])
        icon_prefix = f"{current_style['icon']} " if current_style['icon'] else ""
        
        plain_title_with_icon = f"{icon_prefix}{title}"
        
        h_bar_len = width - 2
        if h_bar_len < 0: h_bar_len = 0


        print(f"{indent_str}{outer_color}{box['VR']}{box['H'] * h_bar_len}{box['VL']}{fmt['END']}")
        
        space_for_title_and_padding = width - 2
        min_side_padding = LAYOUT_CONFIG['HEADER_MIN_PADDING']
        
        max_plain_title_len = space_for_title_and_padding - (min_side_padding * 2)
        truncated_plain_title_with_icon = plain_title_with_icon
        
        title_text_to_display = title
        if len(plain_title_with_icon) > max_plain_title_len:
            if max_plain_title_len > 3 + len(icon_prefix):
                 can_truncate_len = max_plain_title_len - len(icon_prefix) - 3
                 title_text_to_display = title[:can_truncate_len] + "..." if can_truncate_len > 0 else "..."
                 truncated_plain_title_with_icon = f"{icon_prefix}{title_text_to_display}"
            elif max_plain_title_len > len(icon_prefix):
                 title_text_to_display = title[:max_plain_title_len - len(icon_prefix)]
                 truncated_plain_title_with_icon = icon_prefix + title_text_to_display
            else:
                 title_text_to_display = ""
                 truncated_plain_title_with_icon = icon_prefix[:max_plain_title_len]

        formatted_title_colored = f"{title_color_str}{icon_prefix}{title_text_to_display}{fmt['END']}"


        centering_padding_total = space_for_title_and_padding - len(truncated_plain_title_with_icon)
        if centering_padding_total < 0: centering_padding_total = 0
            
        left_pad = centering_padding_total // 2
        right_pad = centering_padding_total - left_pad
        
        print(
            f"{indent_str}{outer_color}{box['V']}{fmt['END']}" 
            f"{' ' * left_pad}{formatted_title_colored}{' ' * right_pad}"
            f"{outer_color}{box['V']}{fmt['END']}")

        print(f"{indent_str}{outer_color}{box['VR']}{box['H'] * h_bar_len}{box['VL']}{fmt['END']}")

    def print_content_box_start(self, width: Optional[int] = None, indent_str: str = LAYOUT_CONFIG['DEFAULT_INDENT_STRING']) -> None:
        width = width or self.config.box_width_medium
        box_h_len = width - 2 
        if box_h_len < 0: box_h_len = 0
        print(f"{indent_str}{self._get_fmt('BOLD')}{self.box['TL']}{self.box['H'] * box_h_len}{self.box['TR']}{self.fmt['END']}")

    def print_content_box_end(self, width: Optional[int] = None, indent_str: str = LAYOUT_CONFIG['DEFAULT_INDENT_STRING']) -> None:
        width = width or self.config.box_width_medium
        box_h_len = width - 2
        if box_h_len < 0: box_h_len = 0
        print(f"{indent_str}{self._get_fmt('BOLD')}{self.box['BL']}{self.box['H'] * box_h_len}{self.box['BR']}{self.fmt['END']}")
        
    def get_wrapped_lines(self, text: str, width: int, initial_indent: str = "", subsequent_indent: str = "") -> List[str]:
        lines = []
        if not text: return [""]
        
        eff_width = max(1, width)

        for paragraph_idx, paragraph in enumerate(text.splitlines()): 
            if not paragraph.strip() and paragraph_idx > 0 : 
                 lines.append(subsequent_indent if lines else initial_indent)
                 continue

            current_paragraph_initial_indent = initial_indent if not lines else subsequent_indent
            

            wrapped_paragraph_lines = textwrap.wrap(
                paragraph, 
                width=eff_width,
                initial_indent="",
                subsequent_indent="",
                replace_whitespace=False, 
                drop_whitespace=True, 
                break_long_words=True,
                break_on_hyphens=True
            )
            
            if not wrapped_paragraph_lines and paragraph:
                lines.append(current_paragraph_initial_indent + paragraph[:eff_width])
            else:
                for i, line_content in enumerate(wrapped_paragraph_lines):
                    prefix_for_this_line = ""
                    if not lines and i == 0:
                        prefix_for_this_line = initial_indent
                    else:
                        prefix_for_this_line = subsequent_indent if i > 0 else current_paragraph_initial_indent
                    
                    lines.append(prefix_for_this_line + line_content)
        return lines


    def print_line_in_box(self, text: str, box_v_char: str, indent_str: str, line_padding: int = 1, color_keys: Tuple[str, ...] = ()):
        padding_str = ' ' * line_padding
        line_color = self._get_fmt(*color_keys) if color_keys else ''
        print(f"{indent_str}{box_v_char}{padding_str}{text}{padding_str}{self.fmt['END']}")


    def print_key_value_in_box(self, key: str, value: Any, box_v_char: str, indent_str: str,
                               key_width: int = 20, key_color_keys: Tuple[str, ...] = ('WHITE', 'BOLD'),
                               value_color_keys: Tuple[str, ...] = ('WHITE',), line_padding: int = 1):
        key_str = f"{self._get_fmt(*key_color_keys)}{key + ':':<{key_width}}{self.fmt['END']}"
        
        if isinstance(value, bool):
            value_str = self.format_boolean(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            if value_color_keys == ('WHITE',):
                value_str = str(value)
            else:
                value_str = f"{self._get_fmt(*value_color_keys)}{value}{self.fmt['END']}"
        else:
            value_str = f"{self._get_fmt(*value_color_keys)}{str(value)}{self.fmt['END']}"
            
        self.print_line_in_box(f"{key_str} {value_str}", box_v_char, indent_str, line_padding)

    def format_boolean(self, value: bool) -> str:
        if value:
            return f"{self._get_fmt('GREEN', 'BOLD')}{self.box['CHECK']}{self.fmt['END']}"
        else:
            return f"{self._get_fmt('RED', 'BOLD')}{self.box['X_MARK']}{self.fmt['END']}"

    def format_status(self, status: str, hwid_erased: bool = False) -> str:
        status_upper = status.upper()
        status_str = status_upper

        if status_upper == "BANNED":
            status_str = f"{self._get_fmt('RED', 'BOLD')}{status_upper}{self.fmt['END']}"
        elif status_upper == "SUSPICIOUS":
            status_str = f"{self._get_fmt('YELLOW', 'BOLD')}{status_upper}{self.fmt['END']}"
        elif status_upper == "CLEAN":
            status_str = f"{self._get_fmt('GREEN', 'BOLD')}{status_upper}{self.fmt['END']}"
        elif status_upper == "UNKNOWN":
            status_str = f"{self._get_fmt('GRAY')}{status_upper}{self.fmt['END']}"
        
        if hwid_erased:
            status_str += f" {self._get_fmt('YELLOW', 'BOLD')}(HWID ERASED){self.fmt['END']}"
        return status_str

    def format_hwid(self, hwid: str) -> str:
        if not isinstance(hwid, str): hwid = str(hwid)
        if hwid.startswith("V2-"):
            prefix = f"{self._get_fmt('BRIGHT_CYAN', 'BOLD')}V2-{self.fmt['END']}"
            base = hwid[3:]
            return f"{prefix}{self._get_fmt('BRIGHT_CYAN')}{base}{self.fmt['END']}"
        return f"{self._get_fmt('BRIGHT_CYAN')}{hwid}{self.fmt['END']}"

    def format_severity(self, severity: str) -> str:
        severity_upper = severity.upper()
        if severity_upper in SEVERITY_LEVELS['HIGH']:
            return f"{self._get_fmt('BRIGHT_RED', 'BOLD')}{severity_upper}{self.fmt['END']}"
        elif severity_upper in SEVERITY_LEVELS['MEDIUM']:
            return f"{self._get_fmt('BRIGHT_YELLOW', 'BOLD')}{severity_upper}{self.fmt['END']}"
        elif severity_upper in SEVERITY_LEVELS['LOW']:
            return f"{self._get_fmt('GREEN', 'BOLD')}{severity_upper}{self.fmt['END']}"
        return severity_upper

    def format_confidence(self, confidence: str) -> str:
        confidence_upper = confidence.upper()
        if confidence_upper in CONFIDENCE_LEVELS['HIGH']:
            return f"{self._get_fmt('GREEN', 'BOLD')}{confidence_upper}{self.fmt['END']}"
        elif confidence_upper in CONFIDENCE_LEVELS['MEDIUM']:
            return f"{self._get_fmt('YELLOW', 'BOLD')}{confidence_upper}{self.fmt['END']}"
        elif confidence_upper in CONFIDENCE_LEVELS['LOW']:
            return f"{self._get_fmt('RED')}{confidence_upper}{self.fmt['END']}"
        return confidence_upper

    def format_count(self, count: int, threshold_medium: Optional[int] = None, threshold_high: Optional[int] = None) -> str:
        cfg_medium = getattr(self.config, 'count_threshold_medium', DEFAULT_REPORT_CONFIG.get('COUNT_THRESHOLD_MEDIUM', 5))
        cfg_high = getattr(self.config, 'count_threshold_high', DEFAULT_REPORT_CONFIG.get('COUNT_THRESHOLD_HIGH', 20))
        
        threshold_medium = threshold_medium if threshold_medium is not None else cfg_medium
        threshold_high = threshold_high if threshold_high is not None else cfg_high


        if count >= threshold_high:
            return f"{self._get_fmt('BRIGHT_RED', 'BOLD')}{count}{self.fmt['END']}"
        elif count >= threshold_medium:
            return f"{self._get_fmt('BRIGHT_YELLOW', 'BOLD')}{count}{self.fmt['END']}"
        elif count > 0 :
             return f"{self._get_fmt('GREEN')}{count}{self.fmt['END']}"
        else: 
            return f"{self._get_fmt('GRAY')}{count}{self.fmt['END']}"


    def truncate_list(self, items: List[str], limit: Optional[int] = None, joiner: str = ", ") -> str:
        if not items:
            return self._get_fmt('GRAY') + "None" + self.fmt['END']

        limit = limit or self.config.truncate_list_limit

        if len(items) <= limit:
            return joiner.join(items)
        
        remaining_count = len(items) - limit
        str_items = [str(item) for item in items[:limit]]
        return joiner.join(str_items) + f"{joiner}{self._get_fmt('GRAY')}and {remaining_count} more...{self.fmt['END']}"


    def truncate_text(self, text: str, max_length: Optional[int] = None) -> str:
        if not text: return ""
        max_length = max_length or self.config.truncate_text_length
        if len(text) > max_length:
            return text[:max_length - 3] + "..."
        return text

    def print_list_items(self, items: List[str],
                         box_v_char: str, 
                         base_indent_str: str, 
                         item_indent_level: int = 1, 
                         prefix_char_key: str = 'BULLET',
                         fmt_key: Optional[str] = None,
                         max_items: Optional[int] = None) -> None:

        if not items:
            line_indent = LAYOUT_CONFIG['DEFAULT_INDENT_STRING'] * item_indent_level
            self.print_line_in_box(f"{line_indent}{self._get_fmt('GRAY')}None{self.fmt['END']}", box_v_char, base_indent_str)
            return

        bullet = self.box.get(prefix_char_key.upper(), prefix_char_key)
        item_color = self.fmt.get(fmt_key.upper(), self._get_fmt('WHITE')) if fmt_key else self._get_fmt('WHITE')
        
        effective_max_items = max_items if max_items is not None else self.config.get_dynamic_limit('LARGE') 

        line_indent_str = LAYOUT_CONFIG['DEFAULT_INDENT_STRING'] * item_indent_level

        for i, item_text in enumerate(items[:effective_max_items]):
            formatted_item_text = f"{item_color}{item_text}{self.fmt['END']}" if fmt_key or item_color != self._get_fmt('WHITE') else item_text
            line_content = f"{line_indent_str}{bullet} {formatted_item_text}"
            self.print_line_in_box(line_content, box_v_char, base_indent_str)

        if len(items) > effective_max_items:
            remaining_count = len(items) - effective_max_items
            line_content = f"{line_indent_str}{bullet} {self._get_fmt('GRAY')}... and {remaining_count} more{self.fmt['END']}"
            self.print_line_in_box(line_content, box_v_char, base_indent_str)

    def print_table_row(self, columns: List[Any], widths: List[int],
                        box_v_char: str, base_indent_str: str,
                        fmt_keys: Optional[List[Optional[Tuple[str, ...]]]] = None) -> None:
        
        if fmt_keys is None:
            fmt_keys_tuples: List[Optional[Tuple[str, ...]]] = [('WHITE',)] * len(columns)
        else:
            fmt_keys_tuples = []
            for fk_idx, fk in enumerate(fmt_keys):
                if fk is None:
                    fmt_keys_tuples.append(('WHITE',))
                elif isinstance(fk, str):
                    fmt_keys_tuples.append((fk,))
                else:
                    fmt_keys_tuples.append(fk)
            if len(fmt_keys_tuples) < len(columns):
                fmt_keys_tuples.extend([('WHITE',)] * (len(columns) - len(fmt_keys_tuples)))


        row_parts = []
        for i, (col_data, width) in enumerate(zip(columns, widths)):
            col_fmt_tuple = fmt_keys_tuples[i] if i < len(fmt_keys_tuples) else ('WHITE',)
            color_prefix = self._get_fmt(*(col_fmt_tuple if col_fmt_tuple is not None else ('WHITE',)))
            
            col_text = str(col_data) 
            current_ansi_len = 0
            if color_prefix:
                 current_ansi_len = len(color_prefix) + len(self.fmt['END'])
            
            text_space = width - current_ansi_len
            if text_space < 0: text_space = 0


            if isinstance(col_data, (int, float)) or (isinstance(col_data, str) and col_data.replace('.', '', 1).isdigit()):
                col_str = f"{color_prefix}{col_text:>{text_space}}{self.fmt['END']}"
            else:
                col_str = f"{color_prefix}{col_text:<{text_space}}{self.fmt['END']}"
            row_parts.append(col_str)

        divider = f" {self._get_fmt('GRAY')}{self.box['V']}{self.fmt['END']} "
        row_str = divider.join(row_parts)
        self.print_line_in_box(row_str, box_v_char, base_indent_str, line_padding=1)


    def print_table_header(self, headers: List[str], widths: List[int],
                           base_indent_str: str, width: Optional[int] = None) -> None:
        box_width = width or self.config.box_width_medium
        h_bar_len = box_width - 2
        if h_bar_len < 0: h_bar_len = 0
        bold_fmt = self._get_fmt('BOLD')

        print(f"{base_indent_str}{bold_fmt}{self.box['TL']}{self.box['H'] * h_bar_len}{self.box['TR']}{self.fmt['END']}")
        self.print_table_row(headers, widths, self.box['V'], base_indent_str, [('WHITE', 'BOLD')] * len(headers))
        print(f"{base_indent_str}{bold_fmt}{self.box['VR']}{self.box['H'] * h_bar_len}{self.box['VL']}{self.fmt['END']}")


    def print_stats_box(self, title: str, stats: Dict[str, Any], 
                        base_indent_str: str = LAYOUT_CONFIG['DEFAULT_INDENT_STRING'], 
                        width: Optional[int] = None, columns: int = 1) -> None:
        box_width = width or self.config.box_width_medium
        
        style_attrs = { 
            'color_keys': ('BRIGHT_CYAN', 'BOLD'), 
            'chars': ('TL', 'TR', 'BL', 'BR', 'H', 'V')
        }
        color_prefix = self._get_fmt(*style_attrs['color_keys'])
        bc_keys = ('TL', 'TR', 'BL', 'BR', 'H', 'V')
        current_box_chars = {key: self.box[val_key] for key, val_key in zip(bc_keys, style_attrs['chars'])}

        h_bar_len = box_width - 2
        if h_bar_len < 0: h_bar_len = 0
        
        print(f"\n{base_indent_str}{color_prefix}{current_box_chars['TL']}{current_box_chars['H'] * h_bar_len}{current_box_chars['TR']}{self.fmt['END']}")

        plain_title = title
        space_for_title_and_padding = box_width - 2
        
        centering_padding_total = space_for_title_and_padding - len(plain_title)
        if centering_padding_total < 0: centering_padding_total = 0
        left_pad = centering_padding_total // 2
        right_pad = centering_padding_total - left_pad
        
        print(
            f"{base_indent_str}{color_prefix}{current_box_chars['V']}{self.fmt['END']}"
            f"{' ' * left_pad}{color_prefix}{title}{self.fmt['END']}{' ' * right_pad}"
            f"{color_prefix}{current_box_chars['V']}{self.fmt['END']}")


        print(f"{base_indent_str}{color_prefix}{self.box['VR']}{current_box_chars['H'] * h_bar_len}{self.box['VL']}{self.fmt['END']}")
        
        content_padding = LAYOUT_CONFIG['STAT_BOX_COLUMN_PADDING'] 
        content_inner_width = box_width - 2 - (content_padding * 2) 
        
        divider_str_colored = f" {self._get_fmt('GRAY')}|{self.fmt['END']}  "
        plain_divider_len = len(" |  ")
        
        col_width_estimate = content_inner_width
        if columns > 1:
             col_width_estimate = (content_inner_width - ((columns - 1) * plain_divider_len)) // columns
        if col_width_estimate <=0: col_width_estimate = 10

        stats_items = list(stats.items())
        rows = (len(stats_items) + columns - 1) // columns

        for row_idx in range(rows):
            row_str_parts_colored = []
            
            for col_idx in range(columns):
                item_idx = row_idx + col_idx * rows
                if item_idx < len(stats_items):
                    key, value = stats_items[item_idx]

                    formatted_key = f"{self._get_fmt('WHITE', 'BOLD')}{key}:{self.fmt['END']}"
                    
                    if isinstance(value, bool): formatted_value = self.format_boolean(value)
                    elif isinstance(value, int) and not isinstance(value, bool): formatted_value = self.format_count(value) 
                    elif isinstance(value, float): formatted_value = f"{self._get_fmt('WHITE')}{value:.2f}{self.fmt['END']}"
                    else: formatted_value = f"{self._get_fmt('WHITE')}{str(value)}{self.fmt['END']}"
                    
                    item_str_colored = f"{formatted_key} {formatted_value}"
                    
                    plain_key_len = len(key) + 1
                    if isinstance(value, bool): plain_value_len = 1
                    elif isinstance(value, (int, float)): plain_value_len = len(str(value).split('.')[0])
                    else: plain_value_len = len(str(value))
                    item_str_plain_len = plain_key_len + 1 + plain_value_len

                    padding_needed = col_width_estimate - item_str_plain_len
                    if padding_needed < 0: padding_needed = 0
                    
                    row_str_parts_colored.append(item_str_colored + ' ' * padding_needed)
                else:
                    row_str_parts_colored.append(' ' * col_width_estimate)
            
            full_row_str = divider_str_colored.join(row_str_parts_colored)
            self.print_line_in_box(full_row_str, current_box_chars['V'], base_indent_str, line_padding=content_padding)

        print(f"{base_indent_str}{color_prefix}{current_box_chars['BL']}{current_box_chars['H'] * h_bar_len}{current_box_chars['BR']}{self.fmt['END']}")

    def print_horizontal_line(self, width: int, indent_str: str = "", char_key: str = 'H', color_keys: Tuple[str, ...] = ('GRAY',)):
        color = self._get_fmt(*color_keys)
        line_char = self.box.get(char_key.upper(), char_key)
        actual_width = width

        print(f"{indent_str}{color}{line_char * actual_width}{self.fmt['END']}")