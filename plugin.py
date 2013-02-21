###
# Copyright (c) 2011-2012, Mike Mueller <mike.mueller@panopticdev.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Do whatever you want
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
###

"""
A Supybot plugin that monitors and interacts with git repositories.
"""

from supybot.commands import optional
from supybot.commands import threading
from supybot.commands import time
from supybot.commands import wrap

import supybot.ircmsgs as ircmsgs
import supybot.callbacks as callbacks
import supybot.schedule as schedule
import supybot.log as log
import supybot.world as world

import ConfigParser
import fnmatch
from functools import wraps
import os
import threading
import time
import traceback

# 'import git' is performed during plugin initialization.
#
# The GitPython library has different APIs depending on the version installed.
# (0.1.x, 0.3.x supported)
GIT_API_VERSION = -1
_DEBUG = False


def log_debug(message):
    ''' Log a debug message based on local debug config. '''
    if _DEBUG:
        log.info("Git: " + message)


def log_info(message):
    ''' Log a info message using plugin framework. '''
    log.info("Git: " + message)


def log_warning(message):
    ''' Log a warning message using plugin framework. '''
    log.warning("Git: " + message)


def log_error(message):
    ''' Log an error message using plugin framework. '''
    log.error("Git: " + message)


def plural(count, singular, plural=None):
    ''' Return singular/plural form of singular arg depending on count. '''
    if count == 1:
        return singular
    if plural:
        return plural
    if singular[-1] == 's':
        return singular + 'es'
    if singular[-1] == 'y':
        return singular[:-1] + 'ies'
    return singular + 's'


def synchronized(tlockname):
    """
    Decorates a class method (with self as the first parameter) to acquire the
    member variable lock with the given name (e.g. 'lock' ==> self.lock) for
    the duration of the function (blocking).
    """

    def _synched(func):
        ''' Wraps the lock. '''

        @wraps(func)
        def _synchronizer(self, *args, **kwargs):
            ''' Implements the locking. '''
            tlock = self.__getattribute__(tlockname)
            tlock.acquire()
            try:
                return func(self, *args, **kwargs)
            finally:
                tlock.release()
        return _synchronizer

    return _synched


def _get_commits(repo, first, last):
    ''' Return list of commits in repo from first to last, inclusive.'''
    if GIT_API_VERSION == 1:
        return repo.commits_between(first, last)
    elif GIT_API_VERSION == 3:
        rev = "%s..%s" % (first, last)
        # Workaround for GitPython bug:
        # https://github.com/gitpython-developers/GitPython/issues/61
        repo.odb.update_cache()
        return repo.iter_commits(rev)
    else:
        raise Exception("Unsupported API version: %d" % GIT_API_VERSION)


class Repository(object):
    "Represents a git repository being monitored."

    def __init__(self, repo_dir, long_name, options):
        """
        Initialize with a repository with the given name and dict of options
        from the config section.
        """

        if GIT_API_VERSION == -1:
            raise Exception("Git-python API version uninitialized.")

        # Validate configuration ("channel" allowed for backward compatibility)
        required_values = ['short name', 'url']
        optional_values = ['branches', 'channel', 'channels', 'commit link',
                           'commit message', 'group header']

        for name in required_values:
            if name not in options:
                raise Exception('Section %s missing required value: %s' %
                        (long_name, name))
        for name, value in options.items():
            if name not in required_values and name not in optional_values:
                raise Exception('Section %s contains unrecognized value: %s' %
                        (long_name, name))

        # Initialize
        self.branches_opt = options.get('branches', ['master'])
        self.branches = []
        self.commit_by_branch = {}
        self.channels = options.get('channels', options.get('channel')).split()
        self.commit_link = options.get('commit link', '')
        self.commit_message = options.get('commit message', '[%s|%b|%a] %m')
        self.errors = []
        header = options.get('group header', 'True')
        self.group_header = header.lower() in ['true', 'yes', '1']
        self.lock = threading.RLock()
        self.long_name = long_name
        self.short_name = options['short name']
        self.repo = None
        self.url = options['url']

        if not os.path.exists(repo_dir):
            os.makedirs(repo_dir)
        self.path = os.path.join(repo_dir, self.short_name)

        # TODO: Move this to GitWatcher (separate thread)
        self.clone()

    @synchronized('lock')
    def clone(self):
        "If the repository doesn't exist on disk, clone it."

        def get_branches(option_val, repo):
            ''' Return list of branches matching users's option_val. '''
            opt_branches = [b.strip() for b in option_val.split()]
            repo.remotes[0].update()
            repo_branches = [r.name.split('/')[1]
                for r in repo.remotes[0].refs if r.is_detached]
            branches = []
            for opt in opt_branches:
                matched = fnmatch.filter(repo_branches, opt)
                if not matched:
                    log_warning("No branch in repository matches " + opt)
                else:
                    branches.extend(matched)
            if not branches:
                log_error("No branch in repository matches: " + option_val)
            return branches

        # pylint: disable=E0602
        if not os.path.exists(self.path):
            git.Git('.').clone(self.url, self.path, no_checkout=True)
        self.repo = git.Repo(self.path)
        self.branches = get_branches(self.branches_opt, self.repo)
        self.commit_by_branch = {}
        for branch in self.branches:
            try:
                if str(self.repo.active_branch) == branch:
                    self.repo.remote().pull(branch)
                else:
                    self.repo.remote().fetch(branch + ':' + branch)
                self.commit_by_branch[branch] = self.repo.commit(branch)
            except:
                log_error("Cannot checkout repo branch: " + branch)

    @synchronized('lock')
    def fetch(self):
        "Contact git repository and update branches appropriately."
        self.repo.remotes[0].update()
        for branch in self.branches:
            if str(self.repo.active_branch) == branch:
                self.repo.remote().pull(branch)
            else:
                self.repo.remote().fetch(branch + ':' + branch)

    @synchronized('lock')
    def get_commit(self, sha):
        "Fetch the commit with the given SHA.  Returns None if not found."
        # pylint: disable=E0602
        try:
            return self.repo.commit(sha)
        except ValueError:    # 0.1.x
            return None
        except git.GitCommandError:    # 0.3.x
            return None

    @synchronized('lock')
    def get_commit_id(self, commit):
        ''' Return the id i. e., the 40-char git sha. '''
        if GIT_API_VERSION == 1:
            return commit.id
        elif GIT_API_VERSION == 3:
            return commit.hexsha
        else:
            raise Exception("Unsupported API version: %d" % GIT_API_VERSION)

    @synchronized('lock')
    def get_new_commits(self):
        '''
        Return dict of commits by branch which are more recent then those
        in self.commit_by_branch
        '''
        new_commits_by_branch = {}
        for branch in self.commit_by_branch:
            result = _get_commits(self.repo,
                                  self.commit_by_branch[branch],
                                  branch)
            results = list(result)
            new_commits_by_branch[branch] = results
            log_debug("Poll: branch: %s last commit: %s, %d commits" %
                          (branch, str(self.commit_by_branch[branch])[:7],
                                       len(results)))
        return new_commits_by_branch

    @synchronized('lock')
    def get_recent_commits(self, branch, count):
        ''' Return count top commits for a branch in a repo. '''
        if GIT_API_VERSION == 1:
            return self.repo.commits(start=branch, max_count=count)
        elif GIT_API_VERSION == 3:
            return list(self.repo.iter_commits(branch))[:count]
        else:
            raise Exception("Unsupported API version: %d" % GIT_API_VERSION)

    @synchronized('lock')
    def format_link(self, commit):
        "Return a link to view a given commit, based on config setting."
        result = ''
        escaped = False
        for c in self.commit_link:
            if escaped:
                if c == 'c':
                    result += self.get_commit_id(commit)[0:7]
                elif c == 'C':
                    result += self.get_commit_id(commit)
                else:
                    result += c
                escaped = False
            elif c == '%':
                escaped = True
            else:
                result += c
        return result

    @synchronized('lock')
    def format_message(self, commit, branch='unknown'):
        """
        Generate an formatted message for IRC from the given commit, using
        the format specified in the config. Returns a list of strings.
        """
        MODE_NORMAL = 0
        MODE_SUBST = 1
        MODE_COLOR = 2
        subst = {
            'a': commit.author.name,
            'b': branch,
            'c': self.get_commit_id(commit)[0:7],
            'C': self.get_commit_id(commit),
            'e': commit.author.email,
            'l': self.format_link(commit),
            'm': commit.message.split('\n')[0],
            'n': self.long_name,
            's': self.short_name,
            'S': ' ',
            'u': self.url,
            'r': '\x0f',
            '!': '\x02',
            '%': '%',
        }
        result = []
        lines = self.commit_message.split('\n')
        for line in lines:
            mode = MODE_NORMAL
            outline = ''
            for c in line:
                if mode == MODE_SUBST:
                    if c in subst.keys():
                        outline += subst[c]
                        mode = MODE_NORMAL
                    elif c == '(':
                        color = ''
                        mode = MODE_COLOR
                    else:
                        outline += c
                        mode = MODE_NORMAL
                elif mode == MODE_COLOR:
                    if c == ')':
                        outline += '\x03' + color
                        mode = MODE_NORMAL
                    else:
                        color += c
                elif c == '%':
                    mode = MODE_SUBST
                else:
                    outline += c
            result.append(outline.encode('utf-8'))
        return result

    @synchronized('lock')
    def record_error(self, e):
        "Save the exception 'e' for future error reporting."
        self.errors.append(e)

    @synchronized('lock')
    def get_errors(self):
        "Return a list of exceptions that have occurred since last get_errors."
        result = self.errors
        self.errors = []
        return result


class Git(callbacks.PluginRegexp):
    "Please see the README file to configure and use this plugin."
    # pylint: disable=R0904

    threaded = True
    unaddressedRegexps = ['_snarf']

    def __init__(self, irc):
        self.init_git_python()
        self.__parent = super(Git, self)
        self.__parent.__init__(irc)
        self.fetcher = None
        self._stop_polling()
        try:
            self._read_config()
        except Exception, e:
            if 'reply' in dir(irc):
                irc.reply('Warning: %s' % str(e))
            else:
                # During bot startup, there is no one to reply to.
                log_warning(str(e))
        self._schedule_next_event()

    def init_git_python(self):
        ''' import git and set GIT_API_VERSION. '''
        global GIT_API_VERSION, git                # pylint: disable=W0602
        try:
            import git
        except ImportError:
            raise Exception("GitPython is not installed.")
        if not git.__version__.startswith('0.'):
            raise Exception("Unsupported GitPython version.")
        GIT_API_VERSION = int(git.__version__[2])
        if not GIT_API_VERSION in [1, 3]:
            log_error('GitPython version %s unrecognized, using 0.3.x API.'
                    % git.__version__)
            GIT_API_VERSION = 3

    def die(self):
        ''' Stop all threads.  '''
        self._stop_polling()
        self.__parent.die()

    def _parse_repo(self, irc, msg, repo, channel):
        """ Parse first parameter as a repo, return repository or None. """
        matches = filter(lambda r: r.short_name == repo, self.repository_list)
        if not matches:
            irc.reply('No repository named %s, showing available:'
                      % repo)
            self.repositories(irc, msg, [])
            return None
        # Enforce a modest privacy measure... don't let people probe the
        # repository outside the designated channel.
        repository = matches[0]
        if channel not in repository.channels:
            irc.reply('Sorry, not allowed in this channel.')
            return None
        return repository

    def repolog(self, irc, msg, args, channel, repo, branch, count):
        """ repo [branch [count]]

        Display the last commits on the named repository. branch defaults
        to 'master', count defaults to 1 if unspecified.
        """
        repository = self._parse_repo(irc, msg, repo, channel)
        if not repository:
            return
        if not branch in repository.branches:
            irc.reply('No such branch being watched: ' + branch)
            irc.reply('Available branches: ' +
                          ', '.join(repository.branches))
            return
        branch_head = repository.get_commit(branch)
        commits = repository.get_recent_commits(branch_head, count)[::-1]
        self._display_commits(irc, channel, repository, commits, 'repolog')

    repolog = wrap(repolog, ['channel',
                             'somethingWithoutSpaces',
                             optional('somethingWithoutSpaces', 'master'),
                             optional('positiveInt', 1)])

    def rehash(self, irc, msg, args):
        """(takes no arguments)

        Reload the Git ini file and restart any period polling.
        """
        self._stop_polling()
        try:
            self._read_config()
            self._schedule_next_event()
            n = len(self.repository_list)
            irc.reply('Git reinitialized with %d %s.' %
                      (n, plural(n, 'repository')))
        except Exception, e:
            irc.reply('Warning: %s' % str(e))

    rehash = wrap(rehash, [])

    def repositories(self, irc, msg, args, channel):
        """(takes no arguments)

        Display the names of known repositories configured for this channel.
        """
        repositories = filter(lambda r: channel in r.channels,
                              self.repository_list)
        if not repositories:
            irc.reply('No repositories configured for this channel.')
            return
        for r in repositories:
            fmt = '\x02%(short_name)s\x02 (%(name)s)'
            irc.reply(fmt % {
                'name': r.long_name,
                'short_name': r.short_name,
                'url': r.url,
            })

    repositories = wrap(repositories, ['channel'])

    def branches(self, irc, msg, args, channel, repo):
        """ <repository name>
        Display the watched branches for a given repository.
        """
        repository = self._parse_repo(irc, msg, repo, channel)
        if not repository:
            return
        irc.reply('Watched branches: ' + ', '.join(repository.branches))

    branches = wrap(branches, ['channel', 'somethingWithoutSpaces'])

    def gitrehash(self, irc, msg, args):
        "Obsolete command, remove this function eventually."
        irc.reply('"gitrehash" is obsolete, please use "rehash".')

    def repolist(self, irc, msg, args):
        "Obsolete command, remove this function eventually."
        irc.reply('"repolist" is obsolete, please use "repositories".')

    def shortlog(self, irc, msg, args):
        "Obsolete command, remove this function eventually."
        irc.reply('"shortlog" is obsolete, please use "log".')

    # Overridden to hide the obsolete commands
    def listCommands(self, pluginCommands=[]):
        return ['repolog', 'rehash', 'repositories', 'branches']

    def _display_some_commits(self, irc, channel,
                              repository, commits, branch):
        "Display a nicely-formatted list of commits for an author/branch."
        commits = list(commits)
        commits_at_once = self.registryValue('maxCommitsAtOnce')
        if len(commits) > commits_at_once:
            irc.queueMsg(ircmsgs.privmsg(channel,
                         "Showing latest %d of %d commits to %s..." % (
                         commits_at_once,
                         len(commits),
                         repository.long_name,
                         )))
        for commit in commits[-commits_at_once:]:
            lines = repository.format_message(commit, branch)
            for line in lines:
                msg = ircmsgs.privmsg(channel, line)
                irc.queueMsg(msg)

    def _display_commits(self, irc, channel,
                         repository, commits_by_branch, ctx='commits'):
        "Display a nicely-formatted list of commits in a channel."

        if not commits_by_branch:
            return
        if not isinstance(commits_by_branch, dict):
            commits_by_branch = {'': commits_by_branch}
        for branch, commits in commits_by_branch.iteritems():
            if not isinstance(commits, list):
                commits_by_branch[branch] = [commits]

        for branch, commits in commits_by_branch.iteritems():
            for a in set([c.author.name for c in commits]):
                commits_ = [c for c in commits if c.author.name == a]
                if not repository.group_header or ctx == 'repolog':
                    self._display_some_commits(irc, channel,
                                               repository, commits_, branch)
                    continue
                if ctx == 'snarf':
                    line = "Talking about %s?" % \
                                repository.get_commit_id(commits_[0])[0:7]
                else:
                    line = "%s pushed %d commit(s) to %s at %s" % (
                        a, len(commits_), branch, repository.short_name)
                msg = ircmsgs.privmsg(channel, line)
                irc.queueMsg(msg)
                self._display_some_commits(irc, channel,
                                           repository, commits_, branch)

    def _poll(self):
        ''' Look for and handle new commits in local copy of repo. '''
        # Note that polling happens in two steps:
        #
        # 1. The GitFetcher class, running its own poll loop, fetches
        #    repositories to keep the local copies up to date.
        # 2. This _poll occurs, and looks for new commits in those local
        #    copies.  (Therefore this function should be quick. If it is
        #    slow, it may block the entire bot.)
        try:
            for repository in self.repository_list:
                # Find the IRC/channel pairs to notify
                targets = []
                for irc in world.ircs:
                    for channel in repository.channels:
                        if channel in irc.state.channels:
                            targets.append((irc, channel))
                if not targets:
                    log_info("Skipping %s: not in configured channel(s)." %
                             repository.long_name)
                    continue

                # Manual non-blocking lock calls here to avoid potentially long
                # waits (if it fails, hope for better luck in the next _poll).
                if repository.lock.acquire(blocking=False):
                    try:
                        errors = repository.get_errors()
                        for e in errors:
                            log_error('Unable to fetch %s: %s' %
                                (repository.long_name, str(e)))
                        new_commits_by_branch = repository.get_new_commits()
                        for irc, channel in targets:
                            self._display_commits(irc, channel, repository,
                                                  new_commits_by_branch)
                        for branch in new_commits_by_branch:
                            repository.commit_by_branch[branch] = \
                               repository.get_commit(branch)
                    except Exception, e:
                        log_error('Exception in _poll repository %s: %s' %
                                (repository.short_name, str(e)))
                    finally:
                        repository.lock.release()
                else:
                    log.info('Postponing repository read: %s: Locked.' %
                        repository.long_name)
            self._schedule_next_event()
        except Exception, e:
            log_error('Exception in _poll(): %s' % str(e))
            traceback.print_exc(e)

    def _read_config(self):
        ''' Read module config file, normally git.ini. '''
        global _DEBUG
        self.repository_list = []
        _DEBUG = self.registryValue('debug')
        repo_dir = self.registryValue('repoDir')
        config = self.registryValue('configFile')
        if not os.access(config, os.R_OK):
            raise Exception('Cannot access configuration file: %s' % config)
        parser = ConfigParser.RawConfigParser()
        parser.read(config)
        for section in parser.sections():
            options = dict(parser.items(section))
            self.repository_list.append(Repository(repo_dir, section, options))

    def _schedule_next_event(self):
        ''' Schedule next run for gitFetcher. '''
        period = self.registryValue('pollPeriod')
        if period > 0:
            if not self.fetcher or not self.fetcher.isAlive():
                self.fetcher = GitFetcher(self.repository_list, period)
                self.fetcher.start()
            schedule.addEvent(self._poll, time.time() + period,
                              name=self.name())
        else:
            self._stop_polling()

    def _snarf(self, irc, msg, match):
        r"""\b(?P<sha>[0-9a-f]{6,40})\b"""
        if not self.registryValue('enableSnarf'):
            return
        sha = match.group('sha')
        channel = msg.args[0]
        repositories = filter(lambda r: channel in r.channels,
                              self.repository_list)
        for repository in repositories:
            commit = repository.get_commit(sha)
            if commit:
                self._display_commits(irc, channel,
                                      repository, commit, 'snarf')
                break

    def _stop_polling(self):
        '''
        Stop  the gitFetcher. Never allow an exception to propagate since

         this is called in die()
        '''
        if self.fetcher:
            try:
                self.fetcher.stop()
                self.fetcher.join()    # This might take time, but it's safest.
            except Exception, e:
                log_error('Stopping fetcher: %s' % str(e))
            self.fetcher = None
        try:
            schedule.removeEvent(self.name())
        except KeyError:
            pass
        except Exception, e:
            log_error('Stopping scheduled task: %s' % str(e))


class GitFetcher(threading.Thread):
    "A thread object to perform long-running Git operations."

    # I don't know of any way to shut down a thread except to have it
    # check a variable very frequently.
    SHUTDOWN_CHECK_PERIOD = 0.1     # Seconds

    # TODO: Wrap git fetch command and enforce a timeout.  Git will probably
    # timeout on its own in most cases, but I have actually seen it hang
    # forever on "fetch" before.

    def __init__(self, repositories, period, *args, **kwargs):
        """
        Takes a list of repositories and a period (in seconds) to poll them.
        As long as it is running, the repositories will be kept up to date
        every period seconds (with a git fetch).
        """
        super(GitFetcher, self).__init__(*args, **kwargs)
        self.repository_list = repositories
        self.period = period * 1.1        # Hacky attempt to avoid resonance
        self.shutdown = False

    def stop(self):
        """
        Shut down the thread as soon as possible. May take some time if
        inside a long-running fetch operation.
        """
        self.shutdown = True

    def run(self):
        "The main thread method."
        # Initially wait for half the period to stagger this thread and
        # the main thread and avoid lock contention.
        end_time = time.time() + self.period / 2
        while not self.shutdown:
            try:
                for repository in self.repository_list:
                    if self.shutdown:
                        break
                    if repository.lock.acquire(blocking=False):
                        try:
                            repository.fetch()
                        except Exception, e:
                            repository.record_error(e)
                        finally:
                            repository.lock.release()
                    else:
                        log_info('Postponing repository fetch: %s: Locked.' %
                                 repository.long_name)
            except Exception, e:
                log_error('Exception checking repository %s: %s' %
                          (repository.short_name, str(e)))
            # Wait for the next periodic check
            while not self.shutdown and time.time() < end_time:
                time.sleep(GitFetcher.SHUTDOWN_CHECK_PERIOD)
            end_time = time.time() + self.period

Class = Git

# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
