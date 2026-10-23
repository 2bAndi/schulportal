import httpx
import pytest

from schulblick.sph.client import LoginLocked, SPHClient, WrongCredentials


def portal(password_ok=True, locked=False):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "start.schulportal.hessen.de" and path == "/":
            return httpx.Response(200, text="<html>Start</html>")
        if host == "login.schulportal.hessen.de":
            body = request.content.decode()
            assert "user=6055.max.muster" in body and "user2=max.muster" in body
            if locked:
                return httpx.Response(200, text='<div id="authErrorLocktime">120</div>')
            if not password_ok:
                return httpx.Response(200, text="<form>Login fehlgeschlagen</form>")
            return httpx.Response(302, headers={"location": "https://connect.schulportal.hessen.de/",
                                                "set-cookie": "SPH-Session=abc; Domain=.hessen.de; Path=/"})
        if host == "connect.schulportal.hessen.de":
            return httpx.Response(302, headers={"location": "https://start.schulportal.hessen.de/index.php?token=t"})
        if host == "start.schulportal.hessen.de" and path == "/index.php":
            return httpx.Response(200, text="<a>logout</a>", headers={"set-cookie": "sid=s123; Path=/"})
        if host == "start.schulportal.hessen.de" and path == "/vertretungsplan.php":
            assert "sid=s123" in request.headers.get("cookie", "")
            return httpx.Response(200, text="<h1>Vertretungsplan für G8a</h1><a>logout</a>")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_login_and_fetch():
    c = SPHClient("6055", "max.muster", "pw", delay=0, transport=portal())
    assert "Vertretungsplan" in c.substitutions_html()
    assert c.logged_in_at is not None


def test_wrong_credentials():
    c = SPHClient("6055", "max.muster", "pw", delay=0, transport=portal(password_ok=False))
    with pytest.raises(WrongCredentials):
        c.login()


def test_locked():
    c = SPHClient("6055", "max.muster", "pw", delay=0, transport=portal(locked=True))
    with pytest.raises(LoginLocked) as exc:
        c.login()
    assert exc.value.seconds == "120"
