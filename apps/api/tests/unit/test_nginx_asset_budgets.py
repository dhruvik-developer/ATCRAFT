"""Static assets never share a per-address budget with the application.

Eager loading made a cold browser cache ask for the whole build at once: about
263 content-hashed chunks per sign-in, multiplexed over the host's HTTP/2 and
forwarded to the deployment's nginx as one request per stream, all from the
plant's one NAT address. Both nginx layers counted those chunks in the budget
the application's own calls draw on - the host's ``main_edge`` (burst 300) and
the deployment's ``limit_conn conn_per_ip 50`` - so a single cold sign-in could
be refused chunks with 503s, and the API calls made alongside them were refused
from the same bucket. A throwaway probe with 150 concurrent chunk downloads
from one address saw 100 refused, and all 10 API calls made meanwhile.

The fix gives ``/assets/`` budgets of its own (sizing in
``infrastructure/nginx/host/atcraft-main.conf`` and
``infrastructure/nginx/conf.d/atcraft.conf``; docs/security.md section 8). The
rule checked here is the one that matters and survives a re-sizing: the location
nginx would choose for a chunk shares no ``limit_req`` or ``limit_conn`` zone
with the locations it would choose for an API call, a sign-in, a document or
the SPA shell. Locations are chosen the way nginx chooses them - exact, then the
longest prefix, ``^~``, then regexes in order - so moving a block around cannot
fool it, and removing the ``/assets/`` location fails it.

Neither layer is visible to CI's browser suite, which reaches the e2e stack over
plain HTTP/1.1 at six connections per host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit

HOST_CONFIG = REPO_ROOT / "infrastructure/nginx/host/atcraft-main.conf"
DEPLOYMENT_CONFIG = REPO_ROOT / "infrastructure/nginx/conf.d/atcraft.conf"

#: One request of each kind the application makes. The chunk name is real.
CHUNK = "/assets/index-Do4f0hdG.js"
APPLICATION_REQUESTS = (
    "/api/v1/items",
    "/api/v1/auth/login",
    "/api/v1/documents/0192f1c4",
    "/",
)

_LIMITS = ("limit_req", "limit_conn")


@dataclass
class Location:
    modifier: str
    path: str
    directives: list[list[str]] = field(default_factory=list)


@dataclass
class Server:
    directives: list[list[str]] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)


def _tokens(text: str) -> list[str]:
    """nginx's lexical units: words, quoted strings, ``{``, ``}`` and ``;``."""
    tokens: list[str] = []
    word: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "#" and not word:
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif char in "\"'":
            end = i + 1
            while end < len(text) and text[end] != char:
                end += 2 if text[end] == "\\" else 1
            word.append(text[i : end + 1])
            i = end + 1
        elif char.isspace() or char in "{};":
            if word:
                tokens.append("".join(word))
                word.clear()
            if not char.isspace():
                tokens.append(char)
            i += 1
        else:
            word.append(char)
            i += 1
    if word:
        tokens.append("".join(word))
    return tokens


def _tree(tokens: list[str], start: int = 0) -> tuple[list[tuple[list[str], list | None]], int]:
    items: list[tuple[list[str], list | None]] = []
    words: list[str] = []
    i = start
    while i < len(tokens):
        token = tokens[i]
        if token == ";":
            items.append((words, None))
            words, i = [], i + 1
        elif token == "{":
            children, i = _tree(tokens, i + 1)
            items.append((words, children))
            words = []
        elif token == "}":
            return items, i + 1
        else:
            words.append(token)
            i += 1
    return items, i


def proxying_servers(text: str) -> list[Server]:
    """Every ``server`` block that proxies something, with its locations."""
    servers: list[Server] = []
    for words, children in _tree(_tokens(text))[0]:
        if words != ["server"] or children is None:
            continue
        server = Server()
        for inner, block in children:
            if inner and inner[0] == "location" and block is not None:
                modifier, path = (inner[1], inner[2]) if len(inner) == 3 else ("", inner[1])
                body = [directive for directive, nested in block if nested is None]
                server.locations.append(Location(modifier, path, body))
            elif block is None:
                server.directives.append(inner)
        if any(d[0] == "proxy_pass" for loc in server.locations for d in loc.directives):
            servers.append(server)
    return servers


def location_for(server: Server, uri: str) -> Location:
    """The location nginx would choose for ``uri``."""
    for location in server.locations:
        if location.modifier == "=" and location.path == uri:
            return location
    prefixes = [
        loc for loc in server.locations if loc.modifier in ("", "^~") and uri.startswith(loc.path)
    ]
    longest = max(prefixes, key=lambda loc: len(loc.path), default=None)
    if longest is not None and longest.modifier == "^~":
        return longest
    for location in server.locations:
        flags = re.IGNORECASE if location.modifier == "~*" else 0
        if location.modifier in ("~", "~*") and re.search(location.path, uri, flags):
            return location
    assert longest is not None, f"no location serves {uri}"
    return longest


def zones(server: Server, location: Location) -> set[str]:
    """The limit zones a request in ``location`` is counted in.

    Each of limit_req and limit_conn is inherited from the server block only
    when the location declares none of its own.
    """
    found: set[str] = set()
    for name in _LIMITS:
        own = [d for d in location.directives if d[0] == name]
        for directive in own or [d for d in server.directives if d[0] == name]:
            argument = next((a for a in directive[1:] if a.startswith("zone=")), directive[1])
            found.add(f"{name}:{argument.removeprefix('zone=')}")
    return found


def shared_budgets(text: str) -> dict[str, set[str]]:
    """Application requests that draw on a zone a static chunk also draws on."""
    shared: dict[str, set[str]] = {}
    for server in proxying_servers(text):
        chunk = zones(server, location_for(server, CHUNK))
        for uri in APPLICATION_REQUESTS:
            overlap = chunk & zones(server, location_for(server, uri))
            if overlap:
                shared.setdefault(uri, set()).update(overlap)
    return shared


# -----------------------------------------------------------------------------
# The configurations
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("config", [HOST_CONFIG, DEPLOYMENT_CONFIG], ids=["host", "deployment"])
def test_a_chunk_shares_no_budget_with_the_application(config) -> None:
    text = config.read_text(encoding="utf-8")
    assert proxying_servers(text), f"{config.name}: no proxying server block found"
    assert not shared_budgets(text), (
        f"{config.relative_to(REPO_ROOT)} counts /assets/ in a budget the application "
        f"uses: {shared_budgets(text)}. A cold cache's burst of chunk downloads would be "
        "refused, and would refuse the API calls made alongside it - give /assets/ its "
        "own zone (docs/security.md section 8)."
    )


def test_the_application_keeps_a_budget_at_both_layers() -> None:
    """Separating the budgets must not have been done by deleting them."""
    for config in (HOST_CONFIG, DEPLOYMENT_CONFIG):
        for server in proxying_servers(config.read_text(encoding="utf-8")):
            api = zones(server, location_for(server, "/api/v1/items"))
            assert any(zone.startswith("limit_req:") for zone in api), config.name


# -----------------------------------------------------------------------------
# The checker sees the defect it exists for
# -----------------------------------------------------------------------------

_DEPLOYMENT_BEFORE = """
map $http_x_request_id $correlation_id { "~^[0-9A-Za-z_.:-]{8,64}$" $http_x_request_id; }
server {
    limit_conn conn_per_ip 50;
    location ~ ^/api/v1/auth/(login|refresh) {
        limit_req zone=api_auth burst=30 nodelay;
        proxy_pass $b;
    }
    location ~ ^/api/v1/documents(?:/|$) { proxy_pass $b; }
    location /api/ { limit_req zone=api_general burst=300 nodelay; proxy_pass $b; }
    location /assets/ { proxy_pass $f; }
    location / { proxy_pass $f; }
}
"""

_HOST_BEFORE = """
server {
    listen 443 ssl;
    location = /healthz { return 200 "ok\\n"; }
    location ^~ /health/ { return 404; }
    location / { limit_req zone=main_edge burst=300 nodelay; proxy_pass http://u; }
    location ~ ^/api/v1/documents(?:/|$) { proxy_pass http://u; }
}
"""


def test_the_checker_finds_assets_inheriting_the_server_connection_budget() -> None:
    shared = shared_budgets(_DEPLOYMENT_BEFORE)
    assert shared["/api/v1/items"] == {"limit_conn:conn_per_ip"}
    assert shared["/api/v1/auth/login"] == {"limit_conn:conn_per_ip"}
    assert shared["/"] == {"limit_conn:conn_per_ip"}


def test_the_checker_finds_assets_falling_through_to_the_application_location() -> None:
    assert shared_budgets(_HOST_BEFORE) == {
        "/api/v1/items": {"limit_req:main_edge"},
        "/api/v1/auth/login": {"limit_req:main_edge"},
        "/": {"limit_req:main_edge"},
    }


def test_the_checker_accepts_assets_with_budgets_of_their_own() -> None:
    fixed_deployment = _DEPLOYMENT_BEFORE.replace(
        "location /assets/ { proxy_pass $f; }",
        "location /assets/ { limit_conn conn_assets_per_ip 1024; proxy_pass $f; }",
    )
    fixed_host = _HOST_BEFORE.replace(
        "location ~ ^/api",
        "location ^~ /assets/ { limit_req zone=main_assets burst=3000 nodelay; "
        "proxy_pass http://u; }\n    location ~ ^/api",
    )
    assert shared_budgets(fixed_deployment) == {}
    assert shared_budgets(fixed_host) == {}


def test_locations_are_chosen_as_nginx_chooses_them() -> None:
    server = proxying_servers(_DEPLOYMENT_BEFORE)[0]
    assert location_for(server, "/api/v1/auth/login").path.startswith("^/api/v1/auth/")
    assert location_for(server, "/api/v1/documents").path.startswith("^/api/v1/documents")
    assert location_for(server, "/api/v1/items").path == "/api/"
    assert location_for(server, CHUNK).path == "/assets/"
    host = proxying_servers(_HOST_BEFORE)[0]
    assert location_for(host, "/health/ready").modifier == "^~"
    assert location_for(host, "/healthz").modifier == "="
    assert location_for(host, CHUNK).path == "/"
