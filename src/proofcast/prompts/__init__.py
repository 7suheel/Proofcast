"""Prompt templates live in ``prompts/*.md`` next to this file, never hardcoded in Python.

Each file has a ``# System`` section and a ``# User`` section. ``{{name}}`` placeholders are
filled by :meth:`PromptTemplate.render` in a single pass, so text we insert (which may contain
``{{...}}`` itself) is never expanded again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_SECTION = re.compile(r"^#\s+(System|User)\s*$", re.MULTILINE)


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    system: str
    user: str

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.system)) | set(_PLACEHOLDER.findall(self.user))

    def render(self, **values: str) -> tuple[str, str]:
        missing = self.placeholders() - values.keys()
        if missing:
            raise PromptError(f"prompt {self.name}: missing values for {sorted(missing)}")

        def fill(text: str) -> str:
            return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)

        return fill(self.system), fill(self.user)


def parse_prompt(name: str, text: str) -> PromptTemplate:
    parts = _SECTION.split(text)
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].lower()] = parts[i + 1].strip()
    if "system" not in sections or "user" not in sections:
        raise PromptError(f"prompt {name}: needs '# System' and '# User' sections")
    return PromptTemplate(name=name, system=sections["system"], user=sections["user"])


@cache
def load_prompt(name: str) -> PromptTemplate:
    """Load ``prompts/<name>.md`` from the package."""
    if not re.fullmatch(r"[a-z_]+", name):
        raise PromptError(f"bad prompt name {name!r}")
    try:
        text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(f"no prompt named {name!r}") from exc
    return parse_prompt(name, text)


def available_prompts() -> list[str]:
    return sorted(
        p.name.removesuffix(".md")
        for p in resources.files(__package__).iterdir()
        if p.name.endswith(".md")
    )
