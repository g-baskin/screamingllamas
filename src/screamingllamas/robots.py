from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .urls import PATH_SAFE, encode_component

_PATTERN_SAFE = PATH_SAFE + "?*"
_TOKEN = re.compile(r"[A-Za-z0-9_*-]+")


def agent_token(user_agent: str) -> str:
    match = _TOKEN.match(user_agent.strip())
    return match.group(0).lower() if match else ""


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str


def _matches(pattern: str, path: str) -> bool:
    # Google's matcher: track every path offset the pattern can reach. O(len * len), and
    # unlike a regex a hostile robots.txt can't make it backtrack exponentially.
    positions = [0]
    last = len(pattern) - 1
    for i, char in enumerate(pattern):
        if char == "$" and i == last:
            return positions[-1] == len(path)
        if char == "*":
            positions = list(range(positions[0], len(path) + 1))
        else:
            positions = [p + 1 for p in positions if p < len(path) and path[p] == char]
            if not positions:
                return False
    return True


# Matching follows RFC 9309 and Google: product-token groups, longest match wins.
class RobotsRules:
    def __init__(
        self, groups: list[tuple[list[str], list[_Rule]]] | None = None, *, blocked: bool = False
    ) -> None:
        self._groups = groups or []
        self._blocked = blocked
        self._cache: dict[str, list[_Rule]] = {}

    @classmethod
    def allow_all(cls) -> RobotsRules:
        return cls()

    @classmethod
    def disallow_all(cls) -> RobotsRules:
        return cls(blocked=True)

    @classmethod
    def parse(cls, text: str) -> RobotsRules:
        groups: list[tuple[list[str], list[_Rule]]] = []
        agents: list[str] = []
        rules: list[_Rule] = []
        has_rules = False
        for raw in text.lstrip("\ufeff").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key in ("user-agent", "useragent"):
                if has_rules:
                    groups.append((agents, rules))
                    agents, rules, has_rules = [], [], False
                agents.append(agent_token(value))
            elif key in ("allow", "disallow") and agents:
                has_rules = True
                if not value:
                    continue
                rules.append(_Rule(key == "allow", encode_component(value, _PATTERN_SAFE)))
        if agents:
            groups.append((agents, rules))
        return cls(groups)

    def _rules_for(self, agent: str) -> list[_Rule]:
        if agent not in self._cache:
            matched = [g for g in self._groups if agent in g[0]]
            if not matched:
                matched = [g for g in self._groups if "*" in g[0]]
            self._cache[agent] = [rule for _, rules in matched for rule in rules]
        return self._cache[agent]

    def is_allowed(self, url: str, agent: str) -> bool:
        if self._blocked:
            return False
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        target = f"{path}?{parts.query}" if parts.query else path
        best: _Rule | None = None
        for rule in self._rules_for(agent.lower()):
            if not _matches(rule.pattern, target):
                continue
            # Longest pattern wins; on a tie, allow beats disallow.
            if best is None or (len(rule.pattern), rule.allow) > (len(best.pattern), best.allow):
                best = rule
        return best is None or best.allow
