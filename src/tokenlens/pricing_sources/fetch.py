"""Bounded, allow-listed HTTP used only by explicit pricing synchronization.

Analysis never calls this module. Only ``tokenlens-azure pricing sync`` and the
guided ``tokenlens-azure foundry`` workflow may, and both continue without a
network result if anything here fails.

Every request is constrained on five axes — host/path allow-list (including
every redirect hop and every pagination continuation), wall-clock timeout,
response size, page/item counts, and retries — so a hostile or broken endpoint
cannot turn a synchronization into an unbounded crawl.

Hitting a ceiling is reported, never absorbed: a truncated feed raises
:class:`BudgetExceededError` so the caller can refuse to publish an incomplete
snapshot instead of mistaking "we stopped reading" for "the meter is absent".
"""

from __future__ import annotations

import email.message
import hashlib
import io
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, NamedTuple
from urllib.parse import urlsplit

__all__ = [
    "AllowListError",
    "AllowListRedirectHandler",
    "BudgetExceededError",
    "FetchBudget",
    "FetchError",
    "FetchedPage",
    "PageEnvelope",
    "PageParse",
    "UrlAllowList",
    "build_urllib_transport",
    "content_sha256",
    "fetch_pages",
    "fetch_text",
    "redirect_probe",
]

USER_AGENT = "tokenlens-for-azure pricing sync (offline analysis tool)"


class FetchError(RuntimeError):
    """A bounded fetch failed. Synchronization must continue without a result."""


class AllowListError(FetchError):
    """A URL was outside the compiled-in allow-list and was never requested."""


class BudgetExceededError(FetchError):
    """A ceiling was reached while the source still had more to give.

    The retrieved data is *incomplete*, which is materially different from a
    complete feed that simply does not publish a meter, so it is never written
    to the cache and never labelled verified.
    """

    def __init__(self, limit: str, message: str) -> None:
        super().__init__(message)
        self.limit = limit


def content_sha256(payload: bytes | str) -> str:
    """Stable SHA-256 of the exact bytes a parser consumed."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class FetchedPage:
    """One successfully retrieved response body.

    ``final_url`` is the URL the response actually came from after any
    redirect. Every hop is allow-list validated, so it is always a URL
    TokenLens was willing to contact.
    """

    url: str
    status: int
    body: bytes
    final_url: str | None = None

    @property
    def resolved_url(self) -> str:
        return self.final_url or self.url

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="strict")


class PageParse(NamedTuple):
    """What one page yielded: its payload, its continuation, and its size."""

    payload: Any
    next_url: str | None
    item_count: int


@dataclass(frozen=True)
class PageEnvelope:
    """One page, parsed exactly once, with its position and content hash."""

    page: FetchedPage
    index: int
    content_hash: str
    payload: Any
    next_url: str | None
    item_count: int


#: ``(url, timeout_seconds, max_bytes) -> FetchedPage``. Injected in tests so
#: the suite never opens a socket.
Transport = Callable[[str, float, int], FetchedPage]

#: ``FetchedPage -> PageParse``. Called once per page by :func:`fetch_pages`.
PageParser = Callable[[FetchedPage], PageParse]


@dataclass(frozen=True)
class UrlAllowList:
    """Exact host and path allow-list. Prefix matching is deliberately not used.

    A pagination link or a redirect target is attacker-influenced data.
    Validating it structurally — scheme, host, port, and exact path — prevents
    a hop to another host, another path, or a non-TLS scheme from ever being
    requested.
    """

    hosts: frozenset[str]
    paths: frozenset[str]
    allowed_ports: frozenset[int] = frozenset({443})

    def validate(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise AllowListError(f"Only https is allowed; refused scheme {parts.scheme!r}.")
        host = (parts.hostname or "").casefold()
        if host not in self.hosts:
            raise AllowListError(f"Host {host or '(none)'!r} is not in the pricing source allow-list.")
        port = parts.port
        if port is not None and port not in self.allowed_ports:
            raise AllowListError(f"Port {port} is not in the pricing source allow-list.")
        if parts.path not in self.paths:
            raise AllowListError(f"Path {parts.path!r} is not in the pricing source allow-list.")
        if parts.username or parts.password:
            raise AllowListError("Credentials embedded in a pricing source URL are refused.")
        return url


@dataclass(frozen=True)
class FetchBudget:
    """Hard ceilings for one synchronization run."""

    timeout_seconds: float = 15.0
    max_pages: int = 40
    max_items: int = 40_000
    max_bytes_per_page: int = 8_000_000
    retries: int = 2
    backoff_seconds: float = 0.5
    sleep: Callable[[float], None] = field(default=time.sleep, compare=False)


class AllowListRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop against the same allow-list.

    urllib follows redirects silently. Without this handler a ``302`` to
    another host — or from ``https`` down to ``http`` — would be fetched even
    though the original allow-list check passed.
    """

    def __init__(self, allow_list: UrlAllowList) -> None:
        self._allow_list = allow_list

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - stdlib signature
        self._allow_list.validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def redirect_probe(
    allow_list: UrlAllowList, *, from_url: str, to_url: str, code: int = 302
) -> urllib.request.Request | None:
    """Run one redirect hop through the handler; raises for a refused target."""
    handler = AllowListRedirectHandler(allow_list)
    return handler.redirect_request(
        urllib.request.Request(from_url),
        io.BytesIO(b""),
        code,
        "Found",
        email.message.Message(),
        to_url,
    )


def build_urllib_transport(allow_list: UrlAllowList) -> Transport:
    """The default transport, bound to the allow-list that governs redirects."""
    opener = urllib.request.build_opener(AllowListRedirectHandler(allow_list))

    def transport(url: str, timeout_seconds: float, max_bytes: int) -> FetchedPage:
        request = urllib.request.Request(  # noqa: S310 - scheme validated by UrlAllowList
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(max_bytes + 1)
                status = int(getattr(response, "status", 200) or 200)
                final_url = str(response.geturl())
        except AllowListError:
            raise
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raise FetchError(f"HTTP {exc.code} from the pricing source.") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:  # pragma: no cover - network path
            raise FetchError(f"The pricing source could not be reached ({type(exc).__name__}).") from exc
        if len(body) > max_bytes:
            raise BudgetExceededError(
                "bytes", f"The pricing source response exceeded {max_bytes} bytes."
            )
        # Belt and braces: the handler already refused a disallowed hop, and
        # the URL actually served is re-checked before anything is parsed.
        return FetchedPage(
            url=url, status=status, body=body, final_url=allow_list.validate(final_url)
        )

    return transport


def _fetch_once(url: str, *, budget: FetchBudget, transport: Transport) -> FetchedPage:
    attempts = max(1, budget.retries + 1)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            page = transport(url, budget.timeout_seconds, budget.max_bytes_per_page)
        except (AllowListError, BudgetExceededError):
            # Neither is transient: retrying would only repeat a refusal.
            raise
        except FetchError as exc:
            last = exc
        except Exception as exc:  # noqa: BLE001 - any transport failure is bounded here
            last = FetchError(f"The pricing source failed ({type(exc).__name__}).")
        else:
            if len(page.body) > budget.max_bytes_per_page:
                raise BudgetExceededError(
                    "bytes",
                    f"The pricing source response exceeded {budget.max_bytes_per_page} bytes.",
                )
            if page.status == 200:
                return page
            last = FetchError(f"HTTP {page.status} from the pricing source.")
        if attempt + 1 < attempts:
            budget.sleep(budget.backoff_seconds * (attempt + 1))
    raise last or FetchError("The pricing source could not be reached.")


def fetch_text(
    url: str,
    *,
    allow_list: UrlAllowList,
    budget: FetchBudget | None = None,
    transport: Transport | None = None,
) -> FetchedPage:
    """Fetch exactly one allow-listed document under a hard budget."""
    budget = budget or FetchBudget()
    allow_list.validate(url)
    return _fetch_once(
        url, budget=budget, transport=transport or build_urllib_transport(allow_list)
    )


def fetch_pages(
    first_url: str,
    *,
    allow_list: UrlAllowList,
    parse: PageParser,
    budget: FetchBudget | None = None,
    transport: Transport | None = None,
) -> Iterator[PageEnvelope]:
    """Follow a server-supplied pagination chain within the compiled budget.

    Each page is parsed exactly once and handed back as a :class:`PageEnvelope`
    carrying its index, its content hash, and its continuation, so no caller
    needs to re-parse a body or key anything by object identity.

    Each continuation URL is re-validated against the same allow-list as the
    first request. A repeated URL ends the walk. Reaching a page or item
    ceiling while a continuation is still pending raises
    :class:`BudgetExceededError`, because the data gathered so far is
    incomplete.
    """
    budget = budget or FetchBudget()
    allow_list.validate(first_url)
    transport = transport or build_urllib_transport(allow_list)
    seen: set[str] = set()
    url = first_url
    items = 0
    for index in range(budget.max_pages):
        seen.add(url)
        page = _fetch_once(url, budget=budget, transport=transport)
        parsed = parse(page)
        items += max(0, parsed.item_count)
        next_url = allow_list.validate(parsed.next_url) if parsed.next_url else None
        yield PageEnvelope(
            page=page,
            index=index,
            content_hash=content_sha256(page.body),
            payload=parsed.payload,
            next_url=next_url,
            item_count=max(0, parsed.item_count),
        )
        if next_url is None or next_url in seen:
            return
        if items >= budget.max_items:
            raise BudgetExceededError(
                "items",
                f"The pricing source returned at least {items} items, reaching the "
                f"{budget.max_items}-item ceiling with more pages still available. "
                "The feed is truncated and must not be published as a complete snapshot.",
            )
        url = next_url
    raise BudgetExceededError(
        "pages",
        f"The pricing source still had more pages after the {budget.max_pages}-page ceiling. "
        "The feed is truncated and must not be published as a complete snapshot.",
    )
