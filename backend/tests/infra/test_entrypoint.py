"""The one branch in the image that decides whether a rate limit can be forged.

``scripts/entrypoint.sh`` chooses between ``--proxy-headers`` and
``--no-proxy-headers`` from a single environment variable, and getting that wrong
does not fail, log, or look different: the per-IP limiters keep answering, keyed
on a value the client now supplies. Replayed against the shape this replaces --
``--proxy-headers --forwarded-allow-ips 127.0.0.1``, unconditionally:

* six sign-in attempts from one address, no ``X-Forwarded-For`` -> ``401, 401,
  401, 429, 429, 429``; the limiter engages;
* eight attempts rotating ``X-Forwarded-For`` -> ``401`` eight times.

So the branch is asserted here rather than read. ``uvicorn`` is replaced on
``PATH`` by a stub that prints its arguments, which is what makes the assertion
about the flags actually passed rather than about the text of the script.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Final

import pytest

_ENTRYPOINT: Final = Path(__file__).resolve().parents[2] / "scripts" / "entrypoint.sh"

_STUB: Final = """#!/bin/sh
echo "$@"
"""


@pytest.fixture(scope="module")
def stub_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A ``PATH`` whose ``uvicorn`` reports its arguments instead of serving."""
    directory = tmp_path_factory.mktemp("entrypoint-stub")
    stub = directory / "uvicorn"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return str(directory)


def _run(stub_path: str, **environment: str) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    assert shell is not None
    return subprocess.run(  # noqa: S603 -- a fixed argv, and the file under test
        [shell, str(_ENTRYPOINT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": f"{stub_path}:{os.environ.get('PATH', '')}", **environment},
    )


def test_the_entrypoint_is_executable_and_shipped() -> None:
    """A ``CMD`` naming a file the image copies without the executable bit does
    not start, and the failure is a build-time one nobody sees until deploy."""
    assert _ENTRYPOINT.is_file()
    assert _ENTRYPOINT.stat().st_mode & stat.S_IXUSR

    containerfile = (_ENTRYPOINT.parents[1] / "Containerfile").read_text(encoding="utf-8")
    assert "COPY --chown=root:root scripts/entrypoint.sh /app/scripts/" in containerfile
    assert 'CMD ["/app/scripts/entrypoint.sh"]' in containerfile


def test_no_configured_proxy_means_forwarded_headers_are_not_read(stub_path: str) -> None:
    """The finding. Without a named proxy nobody verifies ``X-Forwarded-For``, so
    the peer address is the only address, and the flag saying so is explicit --
    uvicorn's own default for ``--proxy-headers`` is *on*, so omitting it would
    have left the limiter keyed on a client-supplied value."""
    result = _run(stub_path)

    assert result.returncode == 0, result.stderr
    assert "--no-proxy-headers" in result.stdout
    assert "--proxy-headers" not in result.stdout.replace("--no-proxy-headers", "")
    assert "--forwarded-allow-ips" not in result.stdout


def test_an_empty_value_is_treated_as_unset(stub_path: str) -> None:
    """``FORWARDED_ALLOW_IPS=`` in an env-file is how "unset" is usually spelled
    by accident, and it must not be read as "trust the empty list, whatever that
    means to uvicorn"."""
    result = _run(stub_path, FORWARDED_ALLOW_IPS="")

    assert result.returncode == 0, result.stderr
    assert "--no-proxy-headers" in result.stdout


def test_a_named_proxy_is_trusted_and_only_that_proxy(stub_path: str) -> None:
    """The deployed shape: ops/chaudron.container pins the proxy's address."""
    result = _run(stub_path, FORWARDED_ALLOW_IPS="10.89.0.2")

    assert result.returncode == 0, result.stderr
    assert "--proxy-headers" in result.stdout
    assert "--no-proxy-headers" not in result.stdout
    assert "--forwarded-allow-ips 10.89.0.2" in result.stdout


def test_a_wildcard_refuses_to_start_rather_than_serving_forgeable_limits(
    stub_path: str,
) -> None:
    """``*`` is the answer the internet gives to this problem. A container that
    does not come up is a deployment that gets fixed; one that comes up with
    every IP-based decision forgeable is not."""
    result = _run(stub_path, FORWARDED_ALLOW_IPS="*")

    assert result.returncode != 0
    assert "uvicorn" not in result.stdout
    assert "FORWARDED_ALLOW_IPS" in result.stderr


def test_the_access_log_stays_off_on_every_branch(stub_path: str) -> None:
    """The other half of the entrypoint, which a rewrite of the first half could
    drop without any test noticing: a request line carries a scanned GTIN and a
    calendar feed identifier into journald."""
    for environment in ({}, {"FORWARDED_ALLOW_IPS": "10.89.0.2"}):
        result = _run(stub_path, **environment)
        assert "--no-access-log" in result.stdout, environment
