from utils.discord_utils import parse_discord_message_link, extract_message_id


class TestParseDiscordMessageLink:
    def test_full_link(self):
        result = parse_discord_message_link(
            "https://discord.com/channels/11111/22222/33333"
        )
        assert result == ("11111", "22222", "33333")

    def test_discordapp_link(self):
        result = parse_discord_message_link(
            "https://discordapp.com/channels/11111/22222/33333"
        )
        assert result == ("11111", "22222", "33333")

    def test_www_prefix(self):
        result = parse_discord_message_link(
            "https://www.discord.com/channels/11111/22222/33333"
        )
        assert result == ("11111", "22222", "33333")

    def test_numeric_only(self):
        result = parse_discord_message_link("12345")
        assert result == (None, None, "12345")

    def test_invalid_link(self):
        result = parse_discord_message_link("https://google.com")
        assert result is None

    def test_empty_string(self):
        result = parse_discord_message_link("")
        assert result is None

    def test_non_discord_link_with_numbers(self):
        result = parse_discord_message_link("https://example.com/111/222/333")
        assert result is None


class TestExtractMessageId:
    def test_from_full_link(self):
        result = extract_message_id(
            "https://discord.com/channels/11111/22222/33333"
        )
        assert result == "33333"

    def test_from_numeric_id(self):
        assert extract_message_id("12345") == "12345"

    def test_empty(self):
        assert extract_message_id("") is None

    def test_none_input(self):
        assert extract_message_id(None) is None

    def test_invalid(self):
        assert extract_message_id("not-a-link") is None
