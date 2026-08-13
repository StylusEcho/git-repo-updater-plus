# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.
import logging
import os
import re
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from glob import glob
from io import StringIO
from urllib.parse import urlsplit

from colorama import Fore, Style
from git import RemoteReference as RemoteRef, Repo, exc

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_JOBS",
    "update_bookmarks",
    "update_directories",
    "run_command",
]

BOLD = Style.BRIGHT
BLUE = Fore.BLUE + BOLD
GREEN = Fore.GREEN + BOLD
RED = Fore.RED + BOLD
CYAN = Fore.CYAN + BOLD
YELLOW = Fore.YELLOW + BOLD
RESET = Style.RESET_ALL

INDENT1 = " " * 3
INDENT2 = " " * 7
INDENT3 = " " * 11
ERROR = RED + "Error:" + RESET

# Matches scp-like remotes ([user@]host:path), but not drive letters or paths:
SCP_LIKE = re.compile(r"(?:[^@/\\]+@)?(?P<host>[^:/\\]{2,}):(?P<path>[^\\]*)\Z")
WEB_SCHEMES = {"http", "https"}
SSH_SCHEMES = {"ssh", "git", "git+ssh", "ssh+git"}

# How many repos we update at once, and how many of those may talk to the same
# host simultaneously. The per-host cap is the important one: it keeps a big
# batch of repos that all live on GitHub from opening a connection per repo.
DEFAULT_JOBS = 8
MAX_JOBS_PER_HOST = 4


class _Output:
    """Buffers the console output for a single repository.

    Repos are updated concurrently, so printing as we go would interleave the
    output of unrelated repos. We also don't know whether a repo is worth
    showing at all until we're done with it: unless the user asks for
    everything, repos without updates or errors are hidden. Lines printed with
    note() are the ones that make a repo worth showing.
    """

    def __init__(self):
        self._buffer = StringIO()
        self.notable = False

    def print(self, *args, **kwargs):
        """Buffer a line of output, as print() would write it."""
        print(*args, file=self._buffer, **kwargs)

    def note(self, *args, **kwargs):
        """Buffer a line that makes this repo worth showing the user."""
        self.notable = True
        self.print(*args, **kwargs)

    def getvalue(self):
        """Return everything buffered so far."""
        return self._buffer.getvalue()


class _HostLimiter:
    """Caps how many fetches may run against a single host at the same time.

    Updating a few hundred repos in parallel would mean a few hundred
    simultaneous connections to whoever hosts them, which is rude at best and
    rate-limited at worst. Fetches from local paths aren't limited.
    """

    def __init__(self, limit):
        self._limit = max(1, limit)
        self._lock = threading.Lock()
        self._semaphores = {}

    @contextmanager
    def hold(self, host):
        """Context manager that holds a slot for the given host, if any."""
        if not host:
            yield
            return
        with self._lock:
            if host not in self._semaphores:
                self._semaphores[host] = threading.Semaphore(self._limit)
            semaphore = self._semaphores[host]
        with semaphore:
            yield


class _Batch:
    """A base path given by the user, and the repositories found inside it."""

    def __init__(self, header=None, repos=()):
        self.header = header
        self.repos = list(repos)
        self.futures = []


class _Session:
    """The settings and shared state behind a single run of updates."""

    def __init__(self, args, total):
        self.args = args
        self.jobs = max(1, min(max(1, args.jobs), total))
        self.limiter = _HostLimiter(min(self.jobs, MAX_JOBS_PER_HOST))
        self.stopping = threading.Event()

        # Repos are only hidden when there are several of them: if the user
        # asked about one repo, they want to hear about it either way.
        self.quiet = total > 1 and not args.show_all

    @contextmanager
    def fetching(self, repo, host):
        """Context manager wrapping a fetch from the given host.

        Besides waiting for a free slot on the host, this stops git from
        prompting on the terminal when we're running several fetches at once,
        since they'd all be reading from it at the same time and the user would
        have no idea which repo was asking. Fetches that need to ask something
        fail instead, and are reported like any other fetch error; running with
        --jobs 1 gets the prompts back.
        """
        with self.limiter.hold(host):
            if self.jobs == 1:
                yield
                return
            ssh_command = os.environ.get("GIT_SSH_COMMAND", "ssh")
            with repo.git.custom_environment(
                GIT_TERMINAL_PROMPT="0",
                GIT_SSH_COMMAND=ssh_command + " -o BatchMode=yes",
            ):
                yield


def _split_remote_url(url):
    """Split a remote URL into a (scheme, host, port, path) tuple, or None.

    Returns None for anything that isn't a network remote (local paths).
    """
    url = url.strip()
    if not url:
        return None

    if "://" not in url:
        scp = SCP_LIKE.match(url)
        if not scp:
            return None  # A local path
        url = "ssh://{0}/{1}".format(scp.group("host"), scp.group("path").lstrip("/"))

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as err:
        logger.debug(err)
        return None

    scheme = parts.scheme.lower()
    if not host or (scheme not in WEB_SCHEMES and scheme not in SSH_SCHEMES):
        return None  # file://, or something else we don't know how to reach
    return scheme, host, port, parts.path


def _get_remote_config_url(remote):
    """Return the configured URL of a remote, or None if it has none."""
    if not remote.config_reader.has_option("url"):
        return None
    return remote.url


def _get_remote_host(remote):
    """Return the hostname a remote fetches from, or None if it's local."""
    url = _get_remote_config_url(remote)
    if not url:
        return None
    split = _split_remote_url(url)
    return split[1] if split else None


def _get_remote_url(remote):
    """Return a browsable URL for the given remote, or None if there is none.

    ssh-style remotes are translated into their https equivalent, so that what
    we print is something terminals will turn into a clickable link. Remotes
    that aren't reachable over the web (local paths) give None. Any credentials
    embedded in the URL are stripped, since we don't want to print those.
    """
    url = _get_remote_config_url(remote)
    if not url:
        return None
    split = _split_remote_url(url)
    if not split:
        return None
    scheme, host, port, path = split

    if ":" in host:  # IPv6 literals lose their brackets when parsed
        host = "[{0}]".format(host)
    if scheme in SSH_SCHEMES:
        scheme = "https"  # The ssh port is meaningless over https, so drop it
    elif port:
        host += ":{0}".format(port)

    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if path and not path.startswith("/"):
        path = "/" + path

    return "{0}://{1}{2}".format(scheme, host, path)


def _fetch_remotes(out, repo, remotes, session):
    """Fetch a list of remotes, reporting what came in along the way."""

    def _get_name(ref):
        """Return the local name of a remote or tag reference."""
        return ref.remote_head if isinstance(ref, RemoteRef) else ref.name

    # TODO: missing branch deleted (via --prune):
    info = [
        ("NEW_HEAD", "new branch", "new branches"),
        ("NEW_TAG", "new tag", "new tags"),
        ("FAST_FORWARD", "branch update", "branch updates"),
    ]
    up_to_date = BLUE + "up to date" + RESET

    for remote in remotes:
        if session.stopping.is_set():
            return
        out.print(INDENT2, "Fetching", BOLD + remote.name, end="")

        if not remote.config_reader.has_option("fetch"):
            out.note(":", YELLOW + "skipped:", "no configured refspec.")
            continue

        try:
            with session.fetching(repo, _get_remote_host(remote)):
                results = remote.fetch(prune=session.args.prune)
        except exc.GitCommandError as err:
            # We should have to do this ourselves, but GitPython doesn't give
            # us a sensible way to get the raw stderr...
            msg = re.sub(r"\s+", " ", err.stderr).strip()
            msg = re.sub(r"^stderr: *'(fatal: *)?", "", msg).strip("'")
            if not msg:
                command = " ".join(shlex.quote(arg) for arg in err.command)
                msg = "{0} failed with status {1}.".format(command, err.status)
            elif not msg.endswith("."):
                msg += "."
            out.note(":", RED + "error:", msg)
            return
        except AssertionError:  # Seems to be the result of a bug in GitPython
            # This happens when git initiates an auto-gc during fetch:
            out.note(
                ":",
                RED + "error:",
                "something went wrong in GitPython,",
                "but the fetch might have been successful.",
            )
            return
        rlist = []
        for attr, singular, plural in info:
            names = [
                _get_name(res.ref) for res in results if res.flags & getattr(res, attr)
            ]
            if names:
                desc = singular if len(names) == 1 else plural
                colored = GREEN + desc + RESET
                rlist.append("{0} ({1})".format(colored, ", ".join(names)))

        if rlist:
            out.note(":", ", ".join(rlist) + ".")
            # Print a clickable link to the remote, like 'git pull' does:
            url = _get_remote_url(remote)
            if url:
                out.print(INDENT3, "From", url)
        else:
            out.print(":", up_to_date + ".")


def _update_branch(out, repo, branch, is_active=False):
    """Update a single branch."""
    out.print(INDENT2, "Updating", BOLD + branch.name, end=": ")
    upstream = branch.tracking_branch()
    if not upstream:
        out.print(YELLOW + "skipped:", "no upstream is tracked.")
        return
    try:
        branch.commit
    except ValueError:
        out.print(YELLOW + "skipped:", "branch has no revisions.")
        return
    try:
        upstream.commit
    except ValueError:
        out.print(YELLOW + "skipped:", "upstream does not exist.")
        return

    try:
        base = repo.git.merge_base(branch.commit, upstream.commit)
    except exc.GitCommandError as err:
        logger.debug(err)
        out.note(YELLOW + "skipped:", "can't find merge base with upstream.")
        return

    if repo.commit(base) == upstream.commit:
        out.print(BLUE + "up to date", end=".\n")
        return

    if is_active:
        try:
            repo.git.merge(upstream.name, ff_only=True)
            out.note(GREEN + "done", end=".\n")
        except exc.GitCommandError as err:
            msg = err.stderr
            if "local changes" in msg and "would be overwritten" in msg:
                out.note(YELLOW + "skipped:", "uncommitted changes.")
            else:
                out.note(YELLOW + "skipped:", "not possible to fast-forward.")
    else:
        status = repo.git.merge_base(
            branch.commit,
            upstream.commit,
            is_ancestor=True,
            with_extended_output=True,
            with_exceptions=False,
        )[0]
        if status != 0:
            out.note(YELLOW + "skipped:", "not possible to fast-forward.")
        else:
            repo.git.branch(branch.name, upstream.name, force=True)
            out.note(GREEN + "done", end=".\n")


def _update_repository(out, repo, session):
    """Update a single git repository by fetching remotes and rebasing/merging.

    The specific actions depend on the arguments given. We will fetch all
    remotes if *args.current_only* is ``False``, or only the remote tracked by
    the current branch if ``True``. If *args.fetch_only* is ``False``, we will
    also update all fast-forwardable branches that are tracking valid
    upstreams. If *args.prune* is ``True``, remote-tracking branches that no
    longer exist on their remote after fetching will be deleted.
    """
    args = session.args
    try:
        active = repo.active_branch
    except TypeError:  # Happens when HEAD is detached
        active = None
    if args.current_only:
        if not active:
            out.note(
                INDENT2,
                ERROR,
                "--current-only doesn't make sense with a detached HEAD.",
            )
            return
        ref = active.tracking_branch()
        if not ref:
            out.note(INDENT2, ERROR, "no remote tracked by current branch.")
            return
        remotes = [repo.remotes[ref.remote_name]]
    else:
        remotes = repo.remotes

    if not remotes:
        out.note(INDENT2, ERROR, "no remotes configured to fetch.")
        return
    _fetch_remotes(out, repo, remotes, session)

    if not args.fetch_only and not session.stopping.is_set():
        for branch in sorted(repo.heads, key=lambda b: b.name):
            _update_branch(out, repo, branch, branch == active)


def _update_repository_task(path, repo_name, session):
    """Update one repository in a worker thread, returning its output."""
    out = _Output()
    out.print(INDENT1, BOLD + repo_name + ":")
    if session.stopping.is_set():
        return out
    try:
        _update_repository(out, Repo(path), session)
    except Exception as err:  # Don't let one bad repo take down the whole run
        logger.debug(err, exc_info=True)
        out.note(INDENT2, ERROR, "{0}: {1}".format(type(err).__name__, err))
    return out


def _run_command(path, repo_name, args):
    """Run an arbitrary shell command on the given repository."""
    out = _Output()
    out.note(INDENT1, BOLD + repo_name + ":")

    cmd = shlex.split(args.command)
    try:
        repo = Repo(path)
        result = repo.git.execute(cmd, with_extended_output=True, with_exceptions=False)
    except exc.GitCommandNotFound as err:
        out.note(INDENT2, ERROR, err)
        return out

    for line in result[1].splitlines() + result[2].splitlines():
        out.note(INDENT2, line)
    return out


def _collect_batch(base_path, args):
    """Find all repositories inside the given base path.

    Determine whether the directory is a git repo on its own, a directory of
    git repositories, a shell glob pattern, or something invalid. If the first,
    the batch contains it alone; if the second or third, it contains all
    repositories inside; if the last, the batch is just an error message.
    """

    def _collect(paths, max_depth):
        """Return all valid repo paths in the given paths, recursively."""
        if max_depth == 0:
            return []

        valid = []
        for path in paths:
            try:
                Repo(path)
                valid.append(path)
            except exc.InvalidGitRepositoryError:
                if not os.path.isdir(path):
                    continue
                children = [os.path.join(path, it) for it in os.listdir(path)]
                valid += _collect(children, max_depth - 1)
            except exc.NoSuchPathError:
                continue
        return valid

    def _get_basename(base, path):
        """Return a reasonable name for a repo path in the given base."""
        if path.startswith(base + os.path.sep):
            return path.split(base + os.path.sep, 1)[1]
        prefix = os.path.commonprefix([base, path])
        while not base.startswith(prefix + os.path.sep):
            old = prefix
            prefix = os.path.split(prefix)[0]
            if prefix == old:
                break  # Prevent infinite loop, but should be almost impossible
        return path.split(prefix + os.path.sep, 1)[1]

    base = os.path.expanduser(base_path)
    max_depth = args.max_depth
    if max_depth >= 0:
        max_depth += 1

    try:
        Repo(base)
        valid = [base]
    except exc.NoSuchPathError:
        if is_comment(base):
            comment = get_comment(base)
            return _Batch(header=CYAN + BOLD + comment if comment else None)
        paths = glob(base)
        if not paths:
            return _Batch(header=" ".join([ERROR, BOLD + base, "doesn't exist!"]))
        valid = _collect(paths, max_depth)
    except exc.InvalidGitRepositoryError:
        if not os.path.isdir(base) or args.max_depth == 0:
            return _Batch(header=" ".join([ERROR, BOLD + base, "isn't a repository!"]))
        valid = _collect([base], max_depth)

    base = os.path.abspath(base)
    suffix = "" if len(valid) == 1 else "s"
    header = "{0} ({1} repo{2}):".format(BOLD + base, len(valid), suffix)

    valid = [os.path.abspath(path) for path in valid]
    paths = [(_get_basename(base, path), path) for path in valid]
    return _Batch(header=header, repos=sorted(paths))


def _print_batch(batch, quiet):
    """Print the results of a batch, waiting on its repos in order.

    Repos finish in whatever order the threads get to them, but they are always
    printed in the order they were found, so output is deterministic. In quiet
    mode, repos with nothing to report are replaced by a summary line.
    """
    if batch.header:
        print(batch.header)

    hidden = 0
    for future in batch.futures:
        out = future.result()
        if out.notable or not quiet:
            print(out.getvalue(), end="")
        else:
            hidden += 1

    if hidden:
        suffix = "" if hidden == 1 else "s"
        summary = "{0} repo{1} up to date".format(hidden, suffix)
        print(INDENT1, BLUE + summary + RESET + ".")


def _update_repos(base_paths, args):
    """Update all repositories in the given base paths, concurrently."""
    batches = [_collect_batch(path, args) for path in base_paths]
    total = sum(len(batch.repos) for batch in batches)
    session = _Session(args, total)

    pool = ThreadPoolExecutor(max_workers=session.jobs)
    try:
        for batch in batches:
            batch.futures = [
                pool.submit(_update_repository_task, path, name, session)
                for name, path in batch.repos
            ]
        for batch in batches:
            _print_batch(batch, session.quiet)
    except BaseException:  # Including a Ctrl-C while we're waiting on a repo
        session.stopping.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()


def is_comment(path):
    """Does the line start with a # symbol?"""
    return path.lstrip().startswith("#")


def get_comment(path):
    """Return the string minus the comment symbol."""
    return path.lstrip().lstrip("#").strip()


def update_bookmarks(bookmarks, args):
    """Loop through and update all bookmarks."""
    if not bookmarks:
        print("You don't have any bookmarks configured! Get help with 'gitup -h'.")
        return

    _update_repos(bookmarks, args)


def update_directories(paths, args):
    """Update a list of directories supplied by command arguments."""
    _update_repos(paths, args)


def run_command(paths, args):
    """Run an arbitrary shell command on all repos.

    Unlike updating, this is done one repo at a time, and everything is
    printed: an arbitrary command's output is the whole point, and running
    unknown commands in parallel is a good way to get surprised.
    """
    for path in paths:
        batch = _collect_batch(path, args)
        if batch.header:
            print(batch.header)
        for name, repo_path in batch.repos:
            print(_run_command(repo_path, name, args).getvalue(), end="")
