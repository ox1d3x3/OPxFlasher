"""Connecting to one phone over ADB and gathering what the other modules need."""

from __future__ import annotations

from dataclasses import dataclass

from opx import profile as profiles
from opx.profile import Profile
from opx.remote import Access, RemoteShell
from opx.tools import Tool


@dataclass
class Session:
    serial: str
    adb: Tool
    shell: RemoteShell
    access: Access
    props: dict[str, str]
    profile: Profile


def open_session(serial: str, adb: Tool | None = None) -> Session:
    adb = (adb or Tool("adb")).bind(serial)
    shell = RemoteShell(adb)
    access = shell.detect_access()
    props = shell.getprops()
    return Session(
        serial=serial,
        adb=adb,
        shell=shell,
        access=access,
        props=props,
        profile=profiles.detect(props),
    )
