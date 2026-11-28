"""What the tool writes to a terminal: text a server, a camera or a USB device chose is inert."""

from __future__ import annotations

import io
import sys

from openmv_ota import cli, terminal


class _Tty(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def test_controls_print_as_question_marks_one_for_one():
    hostile = "ok\x1b]0;owned\x07\rFAKE‮evil​\x9b2J\tend\n"
    out = terminal.shown(hostile)
    assert out == "ok?]0;owned??FAKE?evil??2J\tend\n" and len(out) == len(hostile)
    assert terminal.shown("plain text, tab\tand newline\n") == "plain text, tab\tand newline\n"


def test_a_terminal_gets_the_shown_text_and_a_pipe_the_bytes(monkeypatch):
    tty, pipe = _Tty(), _Tty(tty=False)
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(sys, "stderr", pipe)
    terminal.guard()
    terminal.guard()                                      # idempotent: wrapped once
    assert isinstance(sys.stdout, terminal._Terminal) and sys.stderr is pipe
    assert sys.stdout.write("a\x1b[2Jb") == 6
    print("detail: \x1b[31mred", file=sys.stderr)
    sys.stdout.flush()                                    # the rest is the wrapped stream's
    assert tty.getvalue() == "a?[2Jb" and pipe.getvalue() == "detail: \x1b[31mred\n"
    assert sys.stdout.isatty() is True


def test_a_stream_that_cannot_say_is_left_alone(monkeypatch):
    class _Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    closed = _Closed()
    monkeypatch.setattr(sys, "stdout", closed)
    monkeypatch.setattr(sys, "stderr", None)              # pythonw: no console at all
    terminal.guard()
    assert sys.stdout is closed and sys.stderr is None


def test_main_guards_the_terminal_before_anything_prints(monkeypatch):
    tty = _Tty()
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(sys, "stderr", _Tty(tty=False))
    assert cli.main(["--version"]) == 0
    assert isinstance(sys.stdout, terminal._Terminal)
