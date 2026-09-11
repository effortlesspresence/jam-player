"""
JAM Player 2.0 - How systemd-journald reads what our services write to stderr

Every jam-* unit runs with StandardOutput=journal / StandardError=journal
and no SyslogLevel=, so systemd tags every line a service writes to stderr
with the stream's default priority, 6 (info), regardless of what the line
says. The journald drop-in (etc/systemd/journald.conf.d/jam.conf) sets
MaxLevelStore=warning, and journald applies that to the PRIORITY field,
not to the text. Left alone, a Python "ERROR" line would be stored as an
info message -- i.e. not stored at all.

The way out is systemd's own convention: with SyslogLevelPrefix=yes (the
default for every unit), a line that starts with "<N>" is stored at
priority N and the prefix is stripped before storage, so journalctl output
is unchanged. journald parses the prefix per line, which is why multi-line
output (tracebacks) needs it on every line.

This module is the one place that knows about that. Anything a service
writes to stderr that must survive on the card goes through it. systemd
sets $JOURNAL_STREAM when it has connected stdout/stderr to the journal;
that is the documented way for a program to detect the situation, and it
is how the prefix is switched off when a service is run by hand in a
terminal.

See docs/LOGGING.md.
"""

import logging
import os
from typing import Optional

# Set by systemd (to "<device>:<inode>") when stdout/stderr go to the journal.
JOURNAL_STREAM_ENV = 'JOURNAL_STREAM'

# syslog priorities (sd-daemon.h): emerg=0 ... crit=2 err=3 warning=4
# notice=5 info=6 debug=7.
SYSLOG_CRIT = 2
SYSLOG_ERR = 3
SYSLOG_WARNING = 4
SYSLOG_INFO = 6
SYSLOG_DEBUG = 7

_PRIORITY_BY_LEVEL = (
    (logging.CRITICAL, SYSLOG_CRIT),
    (logging.ERROR, SYSLOG_ERR),
    (logging.WARNING, SYSLOG_WARNING),
    (logging.INFO, SYSLOG_INFO),
)


def stderr_is_journal() -> bool:
    """
    True when systemd connected THIS process's stderr to the journal.

    $JOURNAL_STREAM is "<dev>:<inode>" of the journal socket and is inherited
    by children, so a subprocess whose stderr was redirected to a pipe
    (jam-update's captured commands, TERMINAL_COMMAND) still sees it. Per
    systemd's documentation the check is: the variable is set AND stderr's
    device:inode match it. Only then does the <N> prefix belong on the line.
    """
    raw = os.environ.get(JOURNAL_STREAM_ENV)
    if not raw:
        return False
    try:
        dev, ino = raw.split(':', 1)
        st = os.fstat(2)
        return int(dev) == st.st_dev and int(ino) == st.st_ino
    except Exception:
        return False


def syslog_priority(levelno: int) -> int:
    """
    Map a Python logging level number to a syslog priority.

    Custom levels between the standard ones round down to the nearest
    standard level (25 -> INFO -> 6), so nothing is ever promoted.
    """
    for level, priority in _PRIORITY_BY_LEVEL:
        if levelno >= level:
            return priority
    return SYSLOG_DEBUG


def journal_prefix(text: str, levelno: int) -> str:
    """
    Prefix every non-empty line of `text` with the "<N>" journald priority
    marker for `levelno`.

    Empty lines are left alone: journald would store them as empty info
    entries and then drop them, which loses nothing.
    """
    prefix = f'<{syslog_priority(levelno)}>'
    return '\n'.join(prefix + line if line else line for line in text.split('\n'))


class JournalPriorityFormatter(logging.Formatter):
    """
    A logging.Formatter whose output carries the journald priority prefix.

    Formats exactly like logging.Formatter (same fmt/datefmt semantics,
    same exception and stack rendering) and then prefixes each line with
    "<N>" when stderr is the journal. `journal` forces the behavior either
    way; None means auto-detect via $JOURNAL_STREAM at construction time.
    """

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        journal: Optional[bool] = None,
    ) -> None:
        super().__init__(fmt, datefmt)
        self.journal = stderr_is_journal() if journal is None else bool(journal)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.journal:
            return text
        return journal_prefix(text, record.levelno)
