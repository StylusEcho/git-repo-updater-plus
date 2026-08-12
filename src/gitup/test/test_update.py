# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

import pytest

from gitup.update import _get_remote_url


class _FakeConfigReader:
    def __init__(self, url):
        self._url = url

    def has_option(self, option):
        return option == "url" and self._url is not None


class _FakeRemote:
    """Enough of a git.Remote for _get_remote_url() to work with."""

    def __init__(self, url):
        self.url = url
        self.config_reader = _FakeConfigReader(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        # https remotes are passed through, minus the .git suffix
        (
            "https://github.com/earwig/git-repo-updater.git",
            "https://github.com/earwig/git-repo-updater",
        ),
        (
            "https://github.com/earwig/git-repo-updater",
            "https://github.com/earwig/git-repo-updater",
        ),
        ("http://example.com/foo.git", "http://example.com/foo"),
        ("https://example.com:8443/foo.git", "https://example.com:8443/foo"),
        # Credentials are never printed
        (
            "https://user:token@github.com/earwig/foo.git",
            "https://github.com/earwig/foo",
        ),
        # scp-like and ssh remotes become https links
        (
            "git@github.com:earwig/git-repo-updater.git",
            "https://github.com/earwig/git-repo-updater",
        ),
        ("github.com:earwig/foo.git", "https://github.com/earwig/foo"),
        ("ssh://git@github.com/earwig/foo.git", "https://github.com/earwig/foo"),
        ("ssh://git@example.com:2222/earwig/foo.git", "https://example.com/earwig/foo"),
        ("git://github.com/earwig/foo.git", "https://github.com/earwig/foo"),
        # Trailing slashes are cleaned up
        ("https://example.com/foo/", "https://example.com/foo"),
        # IPv6 literals keep their brackets
        ("ssh://git@[::1]/foo.git", "https://[::1]/foo"),
        # Nothing clickable about a local repository
        ("/home/earwig/repos/foo", None),
        ("../foo", None),
        ("C:\\repos\\foo", None),
        ("file:///home/earwig/repos/foo", None),
        ("", None),
        (None, None),
    ],
)
def test_get_remote_url(url, expected):
    """make sure remotes are converted into clickable links correctly"""
    assert _get_remote_url(_FakeRemote(url)) == expected
