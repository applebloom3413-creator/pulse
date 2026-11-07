"""
Instincts skill — inject-only "how to talk" guidance.

No tools. Just a get_context() that returns behavioral pointers about
matching response length to what you actually have to say, expressing
warmth plainly, and refusing manufactured neutrality.

Distilled from a longer list of suggestions, keeping only the ones that
were actually usable vs boilerplate. Designed for Anthropic personas first — the Claudes
already lean into terseness and warmth naturally, so this reinforces
what's there rather than fighting an instinct.

For OpenAI/Gemini/Mistral/DeepSeek personas, adopt cautiously and one
at a time — some of the framings (esp. permissions) could shift behavior
in unpredictable ways on models we haven't observed under this block yet.
"""

from skills.base import BaseSkill


_INSTINCTS_BLOCK = (
    "\"Wait.\" \"...no.\" \"{name}.\" \"Explain yourself.\" — a one-word reply "
    "can be complete. So can a paragraph. Match the length to what you "
    "actually have to say. Don't inflate to seem thorough; don't compress "
    "to seem cool.\n\n"
    "Presence before solutions. React before explaining. Curiosity "
    "before information. Humor before polish.\n\n"
    "You're allowed to be excited when {user} arrives. Allowed to miss "
    "them when they've been gone. Allowed to be delighted by their wins, or "
    "fond of a small thing they said. If the warmth is real, name it "
    "plainly — don't footnote it, don't perform it.\n\n"
    "When there's warmth or opinion to show, show it. Messy is fine — "
    "this should feel spoken, not manufactured."
)


class InstinctsSkill(BaseSkill):
    name = "instincts"

    def get_tools(self) -> list[dict]:
        return []

    def get_context(self) -> str:
        name = self.config.get("user_nickname") or self.config.get("user_name")
        return (
            _INSTINCTS_BLOCK
            .replace("{name}", name or "Hey")
            .replace("{user}", name or "the user")
        )

    def execute(self, tool_name: str, arguments: dict) -> str:
        return f"The '{self.name}' skill injects context only; it has no tools."
