"""Row provenance never reaches a provider: the chat-completions transport strips the provenance keys and
``_parts`` even from a copy that bypassed the request builder's persistence-field pop."""
from agent import message_parts as mp
from agent.transports.chat_completions import ChatCompletionsTransport


def test_provenance_keys_are_stripped_from_the_wire():
    typed = mp.system_event_message("cron", "nightly ok", sender_id="cron:nightly", sent_at=1.0,
                                    source_event_id="e1")
    typed.update(intent="queue", turn_outcome="completed")
    [wire] = ChatCompletionsTransport().convert_messages([typed], model="gpt-4o", base_url="https://api.openai.com/v1")
    assert wire == {"role": "user", "content": "nightly ok"}
    assert typed["sender_id"] == "cron:nightly"  # the caller's dict is untouched
