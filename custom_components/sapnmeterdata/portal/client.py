"""Async client for SA Power Networks' "Your Meter Data" customer portal.

The portal is a Salesforce Visualforce site with no documented API. The
client signs in through the site's login form, then calls the same Visualforce
remoting methods that the portal's own pages use.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import aiohttp

_LOGGER = logging.getLogger(__name__)

PORTAL_ROOT = "https://customer.portal.sapowernetworks.com.au/"
SITE_PATH = "meterdata/"
LOGIN_PAGE = "CADSiteLogin"
ACCOUNT_PAGE = "CADAccountPage"
DATA_PAGE = "CADRequestMeterData"
LOGIN_FORM = "loginPage:SiteTemplate:siteLogin:loginComponent:loginForm"
REQUIRED_VIEW_STATE_FIELDS = (
    "com.salesforce.visualforce.ViewState",
    "com.salesforce.visualforce.ViewStateMAC",
)
VIEW_STATE_FIELDS = (
    *REQUIRED_VIEW_STATE_FIELDS,
    "com.salesforce.visualforce.ViewStateVersion",
    "com.salesforce.visualforce.ViewStateCSRF",
)
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=120)

# Messages that mean the portal definitely rejected the credentials, as
# opposed to returning the login form for some other reason.
_REJECTION_PATTERNS = (
    r"login attempt has failed",
    r"(?:invalid|incorrect|wrong)\b[^.]{0,40}\b(?:user ?name|email|password)",
    r"(?:user ?name|email) (?:and|or) password",
    r"account (?:has been |is )?(?:locked|disabled|frozen)",
    r"too many (?:failed )?(?:login )?attempts",
)
_NON_INTERVAL_MARKERS = ("basic", "manual", "accumulation")
_INTERVAL_MARKERS = ("interval", "smart", "advanced")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


class SAPNError(Exception):
    """Base class for SAPN portal errors."""


class SAPNAuthError(SAPNError):
    """The portal rejected the email address or password."""


class SAPNConnectionError(SAPNError):
    """The portal could not be reached or returned a server error."""


class SAPNPortalError(SAPNError):
    """The portal responded, but not in the expected way.

    This covers maintenance pages, expired sessions, and changes to the
    portal's pages. These are retried later rather than treated as bad
    credentials.
    """


class SAPNLoginFailedError(SAPNPortalError):
    """Login did not complete, but the portal gave no rejection message."""


class SAPNNoDataError(SAPNError):
    """The portal has no meter data for the requested NMI and dates."""


@dataclass(frozen=True, slots=True)
class MeterAssignment:
    """A meter (NMI) assigned to the signed-in portal account."""

    nmi: str
    description: str | None
    address: str | None
    customer_name: str | None
    business_name: str | None
    meter_type: str | None
    meter_type_description: str | None

    @property
    def name(self) -> str:
        """Return SAPN's best user-facing name for the meter."""
        return next(
            value
            for value in (
                self.description,
                self.business_name,
                self.address,
                self.customer_name,
                self.nmi,
            )
            if value
        )

    @property
    def meter_type_label(self) -> str:
        """Return the portal's meter type for display."""
        return self.meter_type_description or self.meter_type or "Unknown meter type"

    @property
    def supports_interval_data(self) -> bool | None:
        """Return False for basic/manually read meters, None when unknown."""
        normalized = (self.meter_type_description or "").casefold()
        if any(marker in normalized for marker in _NON_INTERVAL_MARKERS):
            return False
        if any(marker in normalized for marker in _INTERVAL_MARKERS):
            return True
        return None

    @classmethod
    def from_portal(cls, raw: Mapping[str, Any]) -> MeterAssignment:
        """Build an assignment from a getNMIAssignments result item."""
        details = raw.get("nmiAssign")
        if not isinstance(details, Mapping):
            details = {}
        nmi = _text(raw.get("theNMI")) or _text(details.get("NMI__c"))
        if nmi is None:
            raise SAPNPortalError("SAPN returned a meter assignment without an NMI")
        return cls(
            nmi=nmi,
            description=_text(raw.get("theDescription"))
            or _text(details.get("NMI_Description__c")),
            address=_text(raw.get("theAddress")),
            customer_name=_text(raw.get("theName"))
            or _text(details.get("Full_Name__c")),
            business_name=_text(raw.get("theBusinessName")),
            meter_type=_text(details.get("Meter_Type__c")),
            meter_type_description=_text(details.get("Meter_Type_Desc__c")),
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class _HiddenInputParser(HTMLParser):
    """Collect hidden ``<input>`` values keyed by id or name."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        attributes = {key: value or "" for key, value in attrs}
        if attributes.get("type", "").lower() != "hidden":
            return
        key = attributes.get("id") or attributes.get("name")
        if key:
            self.fields[key] = attributes.get("value", "")


def hidden_inputs(document: str) -> dict[str, str]:
    """Return the hidden form fields on a page."""
    parser = _HiddenInputParser()
    parser.feed(document)
    return parser.fields


def looks_like_login_page(document: str) -> bool:
    """Return whether a page contains the portal's login form."""
    return (
        "loginComponent:loginForm:username" in document
        and "loginComponent:loginForm:password" in document
    )


def login_rejection_message(document: str) -> str | None:
    """Return the portal's credential-rejection message, if it shows one."""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", document)).split())
    for pattern in _REJECTION_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            start = max(0, text.rfind(".", 0, match.start()) + 1)
            end = text.find(".", match.end())
            return text[start : end + 1 if end >= 0 else None].strip()
    return None


def extract_redirect(document: str) -> str | None:
    """Return the post-login redirect a Visualforce login response issues."""
    match = re.search(
        r"""\.handleRedirect\(\s*['"](?P<url>[^'"]+)['"]\s*\)""",
        document,
    )
    return html.unescape(match.group("url")) if match else None


def _json_objects(document: str) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", document):
        try:
            candidate, _ = decoder.raw_decode(document, match.start())
        except ValueError:
            continue
        if isinstance(candidate, dict):
            yield candidate


def extract_remoting_config(document: str) -> dict[str, Any]:
    """Return the Visualforce remoting configuration embedded in a page."""
    for candidate_document in dict.fromkeys((document, html.unescape(document))):
        for candidate in _json_objects(candidate_document):
            if (
                isinstance(candidate.get("actions"), dict)
                and isinstance(candidate.get("service"), str)
                and isinstance(candidate.get("vf"), dict)
                and candidate["vf"].get("vid")
            ):
                return candidate
    raise SAPNPortalError(
        "The SAPN portal page did not contain its remoting configuration"
    )


def _find_method(config: Mapping[str, Any], method: str) -> tuple[str, dict[str, Any]]:
    for action, action_data in config["actions"].items():
        if not isinstance(action_data, Mapping):
            continue
        for method_data in action_data.get("ms", ()):
            if isinstance(method_data, Mapping) and method_data.get("name") == method:
                return action, dict(method_data)
    raise SAPNPortalError(f"The SAPN portal no longer offers the {method!r} method")


def portal_date(day: date) -> str:
    """Format a date the way the portal's date pickers submit it."""
    return (
        f"{_WEEKDAYS[day.weekday()]}, {day.day:02d} {_MONTHS[day.month - 1]} "
        f"{day.year} 00:00:00 GMT"
    )


class SAPNClient:
    """One signed-in session with the SAPN meter data portal.

    The caller owns ``session``; give each client its own session so that
    portal cookies are not shared.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        portal_root: str = PORTAL_ROOT,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._root = portal_root
        self._site = urljoin(portal_root, SITE_PATH)
        self._login_url = urljoin(self._site, LOGIN_PAGE)
        self._logged_in = False

    async def _request(self, method: str, url: str, **kwargs: Any) -> tuple[str, str]:
        """Send a request and return the response body and final URL."""
        try:
            async with self._session.request(
                method, url, timeout=DEFAULT_TIMEOUT, **kwargs
            ) as response:
                body = await response.text()
                if response.status >= 500:
                    raise SAPNConnectionError(
                        f"SAPN portal returned HTTP {response.status}"
                    )
                if response.status >= 400:
                    raise SAPNPortalError(
                        f"SAPN portal returned HTTP {response.status}"
                    )
                return body, str(response.url)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SAPNConnectionError(
                f"Could not reach the SAPN portal: {err or type(err).__name__}"
            ) from err

    async def login(self) -> None:
        """Sign in to the portal."""
        page, _ = await self._request("GET", self._login_url)
        fields = hidden_inputs(page)
        if not all(fields.get(name) for name in REQUIRED_VIEW_STATE_FIELDS):
            raise SAPNPortalError(
                "The SAPN login page is not showing its login form; "
                "the portal may be down for maintenance"
            )
        form = {
            LOGIN_FORM: LOGIN_FORM,
            f"{LOGIN_FORM}:username": self._email,
            f"{LOGIN_FORM}:password": self._password,
            f"{LOGIN_FORM}:loginButton": "Login",
        }
        form.update(
            {name: fields[name] for name in VIEW_STATE_FIELDS if name in fields}
        )
        response, response_url = await self._request("POST", self._login_url, data=form)

        redirect = extract_redirect(response)
        if redirect is not None:
            landing, _ = await self._request("GET", urljoin(response_url, redirect))
            if looks_like_login_page(landing):
                raise SAPNLoginFailedError(
                    "SAPN returned to the login page after signing in"
                )
        elif looks_like_login_page(response):
            if message := login_rejection_message(response):
                raise SAPNAuthError(message)
            raise SAPNLoginFailedError(
                "SAPN showed the login form again without explaining why"
            )
        self._logged_in = True
        _LOGGER.debug("Signed in to the SAPN portal")

    async def _call(self, page: str, method: str, data: list[Any] | None) -> Any:
        """Call a Visualforce remoting method, signing in again once if needed."""
        if not self._logged_in:
            await self.login()
        page_url = urljoin(self._site, page)
        try:
            return await self._call_once(page_url, method, data)
        except SAPNPortalError as err:
            # Expired sessions and one-off portal hiccups look the same from
            # here, so sign in again and retry once before giving up.
            _LOGGER.debug("Retrying %s after signing in again: %s", method, err)
        await self.login()
        return await self._call_once(page_url, method, data)

    async def _call_once(
        self, page_url: str, method: str, data: list[Any] | None
    ) -> Any:
        document, _ = await self._request("GET", page_url)
        if looks_like_login_page(document):
            raise SAPNPortalError("The SAPN session has expired")
        config = extract_remoting_config(document)
        action, metadata = _find_method(config, method)
        body = {
            "action": action,
            "method": method,
            "type": "rpc",
            "tid": 1,
            "data": data,
            "ctx": {
                "csrf": metadata.get("csrf"),
                "vid": config["vf"]["vid"],
                "ns": metadata.get("ns"),
                "ver": metadata.get("ver"),
                "authorization": metadata.get("authorization"),
            },
        }
        text, _ = await self._request(
            "POST",
            urljoin(self._root, config["service"]),
            json=body,
            headers={"Referer": page_url},
        )
        try:
            reply = json.loads(text)[0]
        except (ValueError, LookupError, TypeError) as err:
            raise SAPNPortalError(
                f"SAPN returned an unreadable response to {method}"
            ) from err
        if not isinstance(reply, Mapping):
            raise SAPNPortalError(f"SAPN returned an unexpected {method} reply")
        status = reply.get("statusCode")
        if reply.get("type") == "exception" or (
            isinstance(status, int) and status >= 400
        ):
            raise SAPNPortalError(
                f"SAPN reported an error for {method}: {reply.get('message') or status}"
            )
        return reply.get("result")

    async def get_assignments(self) -> list[MeterAssignment]:
        """Return the meters assigned to the account."""
        result = await self._call(ACCOUNT_PAGE, "getNMIAssignments", None)
        if not isinstance(result, list):
            raise SAPNPortalError("SAPN returned an unexpected meter list")
        return [
            MeterAssignment.from_portal(item)
            for item in result
            if isinstance(item, Mapping)
        ]

    async def download_nem12(self, nmi: str, first_day: date, last_day: date) -> str:
        """Return the NEM12 text for ``first_day`` to ``last_day`` inclusive.

        The portal may return fewer days than requested, for example when the
        newest day is not published yet.
        """
        result = await self._call(
            DATA_PAGE,
            "downloadNMIData",
            [
                nmi,
                "SAPN",
                portal_date(first_day),
                portal_date(last_day),
                "Customer Access NEM12",
                "Detailed Report (CSV)",
                0,
            ],
        )
        if not isinstance(result, Mapping):
            raise SAPNPortalError(f"SAPN returned an unexpected data reply for {nmi}")
        text = result.get("results")
        if text is None or (isinstance(text, str) and not text.strip()):
            detail = next(
                (
                    str(result[key]).strip()
                    for key in ("message", "errorMessage", "error", "status")
                    if isinstance(result.get(key), str) and result[key].strip()
                ),
                None,
            )
            raise SAPNNoDataError(
                f"SAPN has no data for {nmi} from {first_day} to {last_day}"
                + (f": {detail}" if detail else "")
            )
        if not isinstance(text, str):
            raise SAPNPortalError(f"SAPN returned non-text meter data for {nmi}")
        return text
