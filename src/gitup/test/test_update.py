# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gitup.cli import _build_parser
from gitup.update import (
    _get_remote_host,
    _get_remote_url,
    _HostLimiter,
    update_directories,
)


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


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/earwig/foo.git", "github.com"),
        ("git@github.com:earwig/foo.git", "github.com"),
        ("ssh://git@example.com:2222/earwig/foo.git", "example.com"),
        ("/home/earwig/repos/foo", None),
        (None, None),
    ],
)
def test_get_remote_host(url, expected):
    """make sure we can tell which host a remote will be fetched from"""
    assert _get_remote_host(_FakeRemote(url)) == expected


def test_host_limiter():
    """make sure we don't hit the same host more than the limit allows"""
    limiter = _HostLimiter(2)
    lock = threading.Lock()
    current = {"github.com": 0, "example.com": 0}
    peak = dict(current)

    def _hit(host):
        with limiter.hold(host):
            with lock:
                current[host] += 1
                peak[host] = max(peak[host], current[host])
            for _ in range(1000):  # Long enough for the others to pile up
                pass
            with lock:
                current[host] -= 1

    hosts = ["github.com"] * 20 + ["example.com"] * 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_hit, hosts))

    assert peak["github.com"] <= 2
    assert peak["example.com"] <= 2
    assert all(count == 0 for count in current.values())


def _git(path, *args):
    """Run a git command in the given directory."""
    cmd = ["git", "-C", str(path), "-c", "user.name=gitup", "-c", "user.email=g@i.t"]
    subprocess.run(cmd + list(args), check=True, capture_output=True)


@pytest.fixture
def clones(tmp_path):
    """Build a directory of clones: one with an update, one without."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "file").write_text("one\n")
    _git(upstream, "add", "file")
    _git(upstream, "commit", "-qm", "one")

    clones = tmp_path / "clones"
    clones.mkdir()
    for name in ("repo_current", "repo_updated"):
        _git(clones, "clone", "-q", str(upstream), name)

    (upstream / "file").write_text("two\n")
    _git(upstream, "commit", "-qam", "two")
    _git(clones / "repo_current", "pull", "-q")  # Already has the new commit
    return clones


def test_only_notable_repos_are_printed(clones, capsys):
    """make sure repos with nothing to report are hidden in a multi-repo run"""
    args = _build_parser().parse_args([])
    update_directories([str(clones)], args)
    captured = capsys.readouterr()

    assert "repo_updated" in captured.out
    assert "branch update" in captured.out
    assert "repo_current" not in captured.out
    assert "1 repo up to date" in captured.out


def test_show_all_prints_every_repo(clones, capsys):
    """make sure --all brings back the repos we'd otherwise hide"""
    args = _build_parser().parse_args(["--all"])
    update_directories([str(clones)], args)
    captured = capsys.readouterr()

    assert "repo_updated" in captured.out
    assert "repo_current" in captured.out
    assert "up to date" in captured.out  # The repo we'd otherwise have hidden
    assert "repo up to date" not in captured.out  # No summary line


def test_single_repo_is_always_printed(clones, capsys):
    """make sure a repo named directly is reported on even with no updates"""
    args = _build_parser().parse_args([])
    update_directories([str(clones / "repo_current")], args)
    captured = capsys.readouterr()

    assert "repo_current" in captured.out
    assert "up to date" in captured.out


def test_repos_are_printed_in_order(clones, capsys):
    """make sure concurrent updates don't scramble the output"""
    for name in ("aaa", "zzz"):
        _git(clones, "clone", "-q", str(clones / "repo_updated"), name)
    args = _build_parser().parse_args(["--all", "--jobs", "4"])
    update_directories([str(clones)], args)
    captured = capsys.readouterr()

    names = [name for name in ("aaa", "repo_current", "repo_updated", "zzz")]
    positions = [captured.out.index(name + ":") for name in names]
    assert positions == sorted(positions)
