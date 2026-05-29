class TestComplaintMessage:
    def test_construction(self, sample_complaint_message):
        msg = sample_complaint_message
        assert msg.id == "555555"
        assert msg.content == "Player TestPlayer is breaking rules"
        assert len(msg.embeds) == 1
        assert msg.embeds[0]["title"] == "Evidence"
        assert msg.channel_id == "44444"
        assert msg.guild_id == "33333"
        assert msg.mentioned_nicknames == ["TestPlayer"]

    def test_link_property(self, sample_complaint_message):
        msg = sample_complaint_message
        expected = "https://discord.com/channels/33333/44444/555555"
        assert msg.link == expected

    def test_default_mentioned_nicknames(self):
        from models.complaint import ComplaintMessage
        msg = ComplaintMessage(
            id="1", content="test", embeds=[], channel_id="2", guild_id="3"
        )
        assert msg.mentioned_nicknames == []


class TestComplaintChannel:
    def test_construction(self, sample_complaint_channel):
        ch = sample_complaint_channel
        assert ch.id == "44444"
        assert ch.name == "complaints"
        assert ch.guild_id == "33333"
        assert len(ch.messages) == 1
        assert ch.messages[0].id == "555555"
        assert ch.last_cached_id == "555555"

    def test_default_messages_and_cache(self):
        from models.complaint import ComplaintChannel
        ch = ComplaintChannel(id="1", name="test", guild_id="2")
        assert ch.messages == []
        assert ch.last_cached_id is None
