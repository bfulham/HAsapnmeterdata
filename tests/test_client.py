"""Tests for the SAPN portal client against a local fake portal."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.sapnmeterdata.portal import (
    SAPNAuthError,
    SAPNClient,
    SAPNConnectionError,
    SAPNLoginFailedError,
    SAPNNoDataError,
    SAPNPortalError,
)
from custom_components.sapnmeterdata.portal.client import (
    extract_remoting_config,
    login_rejection_message,
    portal_date,
)

from .common import EMAIL, NMI, PASSWORD

FORM = "loginPage:SiteTemplate:siteLogin:loginComponent:loginForm"
LOGIN_HTML = f"""<html><body><form id="{FORM}" method="post">
<input type="text" name="{FORM}:username" />
<input type="password" name="{FORM}:password" />
<input type="hidden" id="com.salesforce.visualforce.ViewState"
 name="com.salesforce.visualforce.ViewState" value="VIEWSTATE" />
<input type="hidden" id="com.salesforce.visualforce.ViewStateVersion"
 name="com.salesforce.visualforce.ViewStateVersion" value="202609" />
<input type="hidden" id="com.salesforce.visualforce.ViewStateMAC"
 name="com.salesforce.visualforce.ViewStateMAC" value="MAC" />
{{message}}
</form></body></html>"""


def remoting_page(controller: str, method: str) -> str:
    """Return a portal page embedding a Visualforce remoting configuration."""
    config = {
        "vf": {"vid": "066000000000001", "xhr": False},
        "actions": {
            controller: {
                "ms": [
                    {
                        "name": method,
                        "len": 7,
                        "ns": "",
                        "ver": 49.0,
                        "csrf": f"csrf-{method}",
                        "authorization": f"auth-{method}",
                    }
                ],
                "prm": 1,
            }
        },
        "service": "apexremote",
    }
    return (
        "<html><script>Visualforce.remoting.Manager.add("
        f"new $VFRM.RemotingProviderImpl({json.dumps(config)}));</script></html>"
    )


@dataclass
class PortalState:
    """Behaviour of the fake portal."""

    password: str = PASSWORD
    rejection: str = (
        "Your login attempt has failed. "
        "Make sure the username and password are correct."
    )
    maintenance: bool = False
    expire_sessions: int = 0
    nem12: str | None = "200,2001234567,E1,E1,E1,,M,KWH,05,\n900\n"
    logins: int = 0
    rpc_bodies: list[dict[str, Any]] = field(default_factory=list)


def make_app(state: PortalState) -> web.Application:
    """Build a fake SAPN portal."""
    sessions: set[str] = set()

    def signed_in(request: web.Request) -> bool:
        return request.cookies.get("sid") in sessions

    async def login(request: web.Request) -> web.Response:
        if state.maintenance:
            return web.Response(text="<html>Down for maintenance</html>")
        if request.method == "GET":
            return web.Response(text=LOGIN_HTML.format(message=""))
        form = await request.post()
        assert form["com.salesforce.visualforce.ViewState"] == "VIEWSTATE"
        assert form["com.salesforce.visualforce.ViewStateMAC"] == "MAC"
        if (
            form[f"{FORM}:username"] != EMAIL
            or form[f"{FORM}:password"] != state.password
        ):
            return web.Response(
                text=LOGIN_HTML.format(
                    message=f"<span class='error'>{state.rejection}</span>"
                )
            )
        state.logins += 1
        sid = f"session-{state.logins}"
        sessions.add(sid)
        return web.Response(
            text="<script>window.sfdcPage.handleRedirect("
            f"'/meterdata/secur/frontdoor.jsp?sid={sid}&amp;retURL=%2Fmeterdata%2FCADAccountPage');"
            "</script>"
        )

    async def frontdoor(request: web.Request) -> web.Response:
        response = web.HTTPFound("/meterdata/CADAccountPage")
        response.set_cookie("sid", request.query["sid"])
        raise response

    async def page(request: web.Request) -> web.Response:
        if not signed_in(request):
            return web.Response(text=LOGIN_HTML.format(message=""))
        if state.expire_sessions:
            state.expire_sessions -= 1
            sessions.clear()
            return web.Response(text=LOGIN_HTML.format(message=""))
        name = request.match_info["page"]
        if name == "CADAccountPage":
            return web.Response(
                text=remoting_page("CADAccountController", "getNMIAssignments")
            )
        return web.Response(
            text=remoting_page("CADRequestMeterDataController", "downloadNMIData")
        )

    async def apexremote(request: web.Request) -> web.Response:
        body = await request.json()
        state.rpc_bodies.append(body)
        if not signed_in(request):
            return web.json_response(
                [{"statusCode": 402, "type": "exception", "message": "Logged in?"}]
            )
        assert body["ctx"]["csrf"] == f"csrf-{body['method']}"
        if body["method"] == "getNMIAssignments":
            result: Any = [
                {
                    "theNMI": NMI,
                    "theDescription": "HOME",
                    "theAddress": "1 EXAMPLE ST",
                    "nmiAssign": {"Meter_Type_Desc__c": "Interval Meter"},
                },
                {
                    "theNMI": "20099999999",
                    "theAddress": "SHED",
                    "nmiAssign": {"Meter_Type_Desc__c": "Basic Meter"},
                },
            ]
        elif state.nem12 is None:
            result = {"message": "No data found for the selected period"}
        else:
            result = {"results": state.nem12}
        return web.json_response(
            [{"statusCode": 200, "type": "rpc", "tid": 1, "result": result}]
        )

    app = web.Application()
    app.router.add_route("*", "/meterdata/CADSiteLogin", login)
    app.router.add_get("/meterdata/secur/frontdoor.jsp", frontdoor)
    app.router.add_get("/meterdata/{page}", page)
    app.router.add_post("/apexremote", apexremote)
    return app


@pytest.fixture
def state() -> PortalState:
    """Fake portal behaviour."""
    return PortalState()


@pytest.fixture
async def portal_client(
    socket_enabled: None, state: PortalState
) -> AsyncGenerator[SAPNClient]:
    """A client connected to a running fake portal."""
    server = TestServer(make_app(state))
    await server.start_server()
    # The fake portal runs on 127.0.0.1, and aiohttp only keeps cookies from
    # IP addresses when the jar is marked unsafe.
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        yield SAPNClient(
            session, EMAIL, PASSWORD, portal_root=str(server.make_url("/"))
        )
    await server.close()


async def test_login_and_meter_list(
    portal_client: SAPNClient, state: PortalState
) -> None:
    """Signing in follows the redirect and remoting calls use its cookie."""
    await portal_client.login()
    meters = await portal_client.get_assignments()
    assert [meter.nmi for meter in meters] == [NMI, "20099999999"]
    assert meters[0].name == "HOME"
    assert meters[0].supports_interval_data is True
    assert meters[1].name == "SHED"
    assert meters[1].supports_interval_data is False
    assert state.logins == 1
    ctx = state.rpc_bodies[0]["ctx"]
    assert ctx == {
        "csrf": "csrf-getNMIAssignments",
        "vid": "066000000000001",
        "ns": "",
        "ver": 49.0,
        "authorization": "auth-getNMIAssignments",
    }


async def test_download(portal_client: SAPNClient, state: PortalState) -> None:
    """Downloads send the portal's date format and return NEM12 text."""
    text = await portal_client.download_nem12(NMI, date(2026, 9, 1), date(2026, 9, 28))
    assert text == state.nem12
    assert state.rpc_bodies[0]["data"] == [
        NMI,
        "SAPN",
        "Tue, 01 Sep 2026 00:00:00 GMT",
        "Mon, 28 Sep 2026 00:00:00 GMT",
        "Customer Access NEM12",
        "Detailed Report (CSV)",
        0,
    ]


async def test_no_data(portal_client: SAPNClient, state: PortalState) -> None:
    """A reply without results means the portal has no data."""
    state.nem12 = None
    with pytest.raises(SAPNNoDataError, match="No data found"):
        await portal_client.download_nem12(NMI, date(2026, 9, 1), date(2026, 9, 28))


async def test_expired_session_signs_in_again(
    portal_client: SAPNClient, state: PortalState
) -> None:
    """A session that expires mid-run is renewed transparently."""
    await portal_client.login()
    state.expire_sessions = 1
    await portal_client.get_assignments()
    assert state.logins == 2


async def test_rejected_password(portal_client: SAPNClient, state: PortalState) -> None:
    """A rejection message from the portal is an authentication error."""
    state.password = "something else"
    with pytest.raises(SAPNAuthError, match="login attempt has failed"):
        await portal_client.login()


async def test_login_form_without_message(
    portal_client: SAPNClient, state: PortalState
) -> None:
    """The login form returned without a reason is not treated as bad credentials."""
    state.password = "something else"
    state.rejection = ""
    with pytest.raises(SAPNLoginFailedError):
        await portal_client.login()


async def test_maintenance_page(portal_client: SAPNClient, state: PortalState) -> None:
    """A maintenance page is a temporary portal error, not an auth failure."""
    state.maintenance = True
    with pytest.raises(SAPNPortalError) as err:
        await portal_client.login()
    assert not isinstance(err.value, SAPNLoginFailedError)


async def test_unreachable_portal(socket_enabled: None) -> None:
    """Connection failures are reported as connection errors."""
    async with aiohttp.ClientSession() as session:
        client = SAPNClient(session, EMAIL, PASSWORD, portal_root="http://127.0.0.1:9/")
        with pytest.raises(SAPNConnectionError):
            await client.login()


def test_remoting_config_extraction() -> None:
    """The remoting configuration is found regardless of key order or escaping."""
    page = remoting_page("Controller", "downloadNMIData").replace('"', "&quot;")
    config = extract_remoting_config(page)
    assert config["service"] == "apexremote"
    with pytest.raises(SAPNPortalError):
        extract_remoting_config('<html>{"not": "it"}</html>')


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            "<div>Your login attempt has failed. Make sure the username and "
            "password are correct.</div>",
            "Your login attempt has failed.",
        ),
        ("<p>Invalid email or password.</p>", "Invalid email or password."),
        ("<p>Your account has been locked.</p>", "Your account has been locked."),
        (LOGIN_HTML.format(message=""), None),
    ],
)
def test_login_rejection_message(document: str, expected: str | None) -> None:
    """Rejection messages are recognised; a plain login form is not one."""
    assert login_rejection_message(document) == expected


def test_portal_date_is_locale_independent() -> None:
    """Dates use English names whatever the system locale."""
    assert portal_date(date(2026, 10, 4)) == "Sun, 04 Oct 2026 00:00:00 GMT"
