#!/usr/bin/env python3
"""PreToolUse guard of the task loop (core file: do not edit).

Claude Code calls this script before every matched tool call, passing the event as JSON on
stdin. Exit code 0 lets the call proceed through the normal permission flow; exit code 2 blocks
it and the stderr text is shown to the agent.

What it enforces
----------------
- Emergency stop: while a file named AGENT_EMERGENCY_STOP exists at the repository root, every
  tool call is blocked.
- Stop: while a file named AGENT_STOP exists, the loop winds down. A task agent may only commit
  its work in progress and write a ``paused`` claim with a handoff note; every other call it makes
  is refused with that instruction. The reviewer, resolver and challenger are told to end without
  writing; the orchestrator may run the step scripts but spawn nothing. Owner sessions go on.
- Write permissions per role (Write, Edit, MultiEdit, NotebookEdit, and a best-effort analysis of
  shell commands), lock markers of lockable directories, and the protected paths outside the
  repository (read-only for every role, the owner included).
- Read restrictions per role (``deny_read``) for Read, Grep, Glob and shell commands, including
  recursive searches of a directory that contains hidden files.
- Session binding: a main conversation is ``owner`` until it runs ``harness/step_next.py``; from
  then on that session (by ``session_id``) is the ``orchestrator``, until the owner types a prompt
  in it (harness/prompt_hook.py releases the binding). Other sessions stay ``owner``.
- Dispatch control for the orchestrator: it may spawn only the subagent the step scripts expect
  (``state.current.spawn``), in the foreground, with the prompt they generated.

Role of a tool call: inside a subagent whose name is a role, that role; otherwise the role of the
main conversation (``pathrules.effective_role``). Rules: harness/roles.json, harness/project.json,
harness/pathrules.py.

Fail closed: any error inside this script (unreadable input or state, unexpected exception)
blocks the call with exit code 2. Claude Code treats any other non-zero exit code as a
non-blocking error and would let the call through.

Limitations
-----------
Shell analysis is heuristic: a Python script that opens a file is not detected here. Here-document
bodies and PowerShell here-strings are data (strip_heredocs); what cannot be delimited is blocked. Writes are
re-checked by step_verify.py on the git diff of every task; reads have no second layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(PROJECT_DIR / "harness"))

# Windows consoles default to a legacy code page: a message containing a character it
# cannot encode would crash the guard with exit code 1, which Claude Code ignores.
sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

try:
    import pathrules  # noqa: E402
    import statefile  # noqa: E402
except Exception as _exc:  # noqa: BLE001 - fail closed if the rule engine cannot load
    print(f"Guard could not load harness/pathrules.py ({_exc!r}); failing closed. Tell the owner.",
          file=sys.stderr)
    sys.exit(2)

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
AGENT_TOOLS = {"Agent", "Task"}
STATE_FILE = PROJECT_DIR / "harness" / "state.json"
OWNER_WRITES = PROJECT_DIR / "harness" / "owner_writes.json"
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&", "{", "}"}
REDIRECTS_OUT = {">", ">>", ">|"}
REDIRECTS_IN = {"<", "<<", "<<<"}
WRAPPERS = {"sudo", "env", "nohup", "time", "command", "xargs", "timeout", "nice"}
WRITE_ALL_ARGS = {
    "rm", "rmdir", "mv", "touch", "truncate", "chmod", "tee", "unlink",
    # PowerShell cmdlets and aliases (matched case-insensitively)
    "set-content", "add-content", "out-file", "new-item", "remove-item", "move-item",
    "rename-item", "clear-content", "tee-object", "sc", "ac", "ni", "ri", "del", "erase",
    "rd", "ren", "move", "mi", "clc", "set-item", "si", "clear-item", "cli",
    "export-csv", "epcsv", "export-clixml", "expand-archive", "compress-archive",
    # Copy-Item takes -Destination anywhere on the line: check every path (fail closed)
    "copy-item", "copy", "cpi",
}
WRITE_LAST_ARG = {"cp", "install", "ln"}
SHELL_TOOLS = {"Bash", "PowerShell"}
SEARCH_TOOLS = {"Grep", "Glob"}
# Shell commands that read every file under a directory operand (always, or with a recursive
# flag): for roles with deny_read, a directory containing hidden files is refused.
RECURSIVE_ALWAYS = {"rg", "ag", "ack", "tar", "zip", "7z", "robocopy", "xcopy", "du", "tree"}
RECURSIVE_WITH_FLAG = {"grep", "egrep", "fgrep", "cp", "ls", "dir", "get-childitem", "gci",
                       "select-string", "sls", "findstr", "copy-item", "cpi", "find", "diff"}
# compared with the lowered argument (so "-R" is "-r"; "ls -r" is refused too: fail closed)
RECURSIVE_FLAGS = {"-r", "--recursive", "-recurse", "/s", "--dereference-recursive"}
HEARTBEAT = PROJECT_DIR / "harness" / "logs" / "guard_heartbeat.json"
GIT_FORBIDDEN = {"reset", "checkout", "restore", "clean", "stash", "rebase", "push", "switch",
                 "merge", "pull", "cherry-pick", "revert", "branch", "tag"}
# Command that starts (or continues) the loop: it binds the calling session as orchestrator.
STEP_NEXT = re.compile(r"^(python3?|py|\.venv/bin/python3?|\.venv/Scripts/python(\.exe)?)"
                       r" harness/step_next\.py$")
# A harness script named anywhere in a command, with the words that follow it.
HARNESS_SCRIPT = re.compile(r"harness[/\\]+(step_\w+|owner_tools)\.py\b([^;&|\n]*)", re.IGNORECASE)
# git global options that take a separate value (git -C <dir> reset ...)
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                       "--config-env", "--exec-path"}
POSIX_SHELLS = {"bash", "sh", "zsh", "dash"}
POWERSHELLS = {"powershell", "pwsh"}
# Commands whose effect cannot be analysed statically: denied to every role except owner.
OPAQUE_COMMANDS = {
    "iex", "invoke-expression", "invoke-command", "icm", "start-process", "saps", "start",
    "start-job", "sajb", "invoke-item", "ii",
}
DOTNET_FILE_API = re.compile(
    r"\[\s*(?:system\.)?(?:io\.)?(?:file|directory|fileinfo|directoryinfo|filestream|streamwriter"
    r")\s*\]\s*::|new-object\s+(?:-typename\s+)?(?:system\.)?io\.",
    re.IGNORECASE,
)
NULL_DEVICES = {"/dev/null", "$null", "nul"}  # compared case-insensitively
ENV_REF = re.compile(r"\$env:(\w+)|\$\{(\w+)\}|\$(\w+)|%(\w+)%", re.IGNORECASE)

READ_TOOLS = {"Read", "Grep", "Glob"}
# AGENT_STOP: the start of every denial (step_verify looks for it) and what a task agent may still run.
STOP_PREFIX = "AGENT_STOP is present"
STOP_GIT = {"add", "commit", "status", "diff", "log", "show", "rev-parse"}
STOP_MESSAGE_COMMANDS = {"cat", "echo", "printf"}  # inside a $(...) commit message only
STOP_TASK_MESSAGE = (
    f"{STOP_PREFIX}: the owner is stopping the loop and cannot wait for this task to finish. Wind "
    "down now, in this order, with no other tool call (any other call is refused): 1. commit your "
    "work in progress as it is, even if incomplete or failing (git add the files of the task, then "
    "git commit -m \"<task id>: WIP <what it contains>\"); 2. write harness/claim.json with "
    '{"task_id": "<task id>", "status": "paused", "done": "what is done and committed", '
    '"todo": "what is left, the next step first, and anything the next session must know"}; '
    "3. end your turn. The task is not counted as an attempt: the next session starts from your "
    "commits and this note."
)

FIX_HINT = (
    " Do not work around this guard (no redirection, no scripts writing the file, "
    "no renames). If you believe the change is required, set your claim to 'blocked' "
    "and explain the needed change in the 'question' field for the owner."
)


BLOCKS_LOG = PROJECT_DIR / "harness" / "logs" / "guard_blocks.jsonl"
BLOCKS_KEEP = 200  # entries kept in the log
LOG_FIELD_CAP = 4096  # characters kept of each text field of a blocked call (content, command)
#: The call being checked, for the blocks log: set by run() once the role is known.
CALL: dict = {}
#: Set by harness/session_check.py on the guard it runs on fake events: those blocks are the
#: self-test, not calls of an agent, and are neither logged nor taken as the heartbeat.
SELFTEST = os.environ.get("LAOP_GUARD_SELFTEST") == "1"


def deny(message: str) -> None:
    """Block the tool call and show the message to the agent."""
    print(message, file=sys.stderr)
    log_block(message)
    sys.exit(2)


def log_block(message: str) -> None:
    """Record a blocked call of a loop role in harness/logs/guard_blocks.jsonl: the step scripts
    tell a reviewer that wrote nothing from one the guard stopped, and the owner sees who made the
    call and what it tried to write. Never fails the deny."""
    if SELFTEST or not CALL or CALL.get("role") == pathrules.OWNER_ROLE:
        return
    try:
        BLOCKS_LOG.parent.mkdir(parents=True, exist_ok=True)
        lines = BLOCKS_LOG.read_text(encoding="utf-8").splitlines() if BLOCKS_LOG.exists() else []
        lines = [*lines[-(BLOCKS_KEEP - 1):], json.dumps({**CALL, "time": time.time(),
                                                           "message": message[:LOG_FIELD_CAP]})]
        BLOCKS_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - logging must never let the call through
        pass


def logged_text(value) -> dict:
    """Return a text field of a blocked call as logged.

    The first LOG_FIELD_CAP characters, the full length and the sha256 of the full text (to
    compare two attempts without keeping them whole).
    """
    text = value if isinstance(value, str) else json.dumps(value)
    return {"text": text[:LOG_FIELD_CAP], "length": len(text), "truncated": len(text) > LOG_FIELD_CAP,
            "sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()}


def call_details(event: dict, tool: str, tool_input: dict, cwd: str | None) -> dict:
    """Return what the blocks log records of a call besides its role.

    Who made it (session, agent, transcript), and for writes and shell commands what it tried
    to do.
    """
    details = {key: event.get(key) for key in ("session_id", "agent_id", "hook_event_name",
                                                 "transcript_path", "permission_mode")}
    details["cwd"] = cwd
    if tool in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        details["path"] = path
        try:
            details["path_resolved"] = pathrules._absolute(path, PROJECT_DIR, cwd) if path else ""
        except (OSError, ValueError):
            details["path_resolved"] = None
        content = {}
        for key in ("content", "old_string", "new_string", "new_source"):
            if tool_input.get(key) is not None:
                content[key] = logged_text(tool_input[key])
        if tool_input.get("edits") is not None:  # MultiEdit
            content["edits"] = logged_text(tool_input["edits"])
        details["content"] = content
    elif tool in SHELL_TOOLS:
        details["command"] = logged_text(tool_input.get("command") or "")
    return details


class Unparsable(ValueError):
    """Text whose extent as a command cannot be determined (an unclosed quote, here-document,
    here-string or substitution): the guard blocks it (fail closed)."""


# '#' starts a comment only at the start of a word, after one of these (or at the start).
_COMMENT_AFTER = " \t\r\n;&|()"
# Characters that end the delimiter word of a here-document.
_WORD_END = " \t\r\n;&|()<>"


class _Scanner:
    """Separates, in a shell command, the text the shell parses as commands from the text it does
    not: here-document bodies and PowerShell here-strings, quoted strings, comments. Commands
    substituted where the shell expands text (``$(...)``, POSIX backticks) are collected in
    ``nested`` to be checked on their own.

    ``lenient`` (the owner's commands): a here-document without its closing line runs to the end
    of the command, as bash reads it (with a warning), instead of making the command unparsable."""

    def __init__(self, text: str, windows_syntax: bool, lenient: bool = False):
        self.s, self.n, self.win, self.lenient = text, len(text), windows_syntax, lenient
        self.esc = "`" if windows_syntax else "\\"
        self.nested: list[str] = []

    def substitution(self, i: int) -> int:
        """At '$(': record the substituted command; return the index after its ')'.
        Arithmetic ``$((...))`` is not a command, but may contain substitutions of its own."""
        _, j = self.scan(i + 2, until_paren=True)
        inner = self.s[i + 2:j]
        if inner.startswith("(") and inner.endswith(")"):
            arithmetic = _Scanner(inner[1:-1], self.win, self.lenient)
            arithmetic.expanded(0, len(inner) - 2)
            self.nested += arithmetic.nested
        else:
            self.nested.append(inner)
        return j + 1

    def backtick(self, i: int) -> int:
        """At a POSIX backtick: record the substituted command; return the index after it."""
        j = i + 1
        while j < self.n and self.s[j] != "`":
            j += 2 if self.s[j] == "\\" else 1
        if j >= self.n:
            raise Unparsable("a backtick substitution is not closed")
        self.nested.append(self.s[i + 1:j])
        return j + 1

    def expanded(self, i: int, end: int, closing: str = "") -> tuple[str, int]:
        """Text the shell expands without parsing it as commands: a double-quoted string (up to
        ``closing``), an unquoted here-document body, a ``@"`` here-string. Returns the text with
        each substitution replaced by ``$SUBST``, and the index after it."""
        out: list[str] = []
        while i < end:
            c = self.s[i]
            if closing and c == closing:
                return "".join(out), i + 1
            if c == self.esc:
                out.append(self.s[i:i + 2])
                i += 2
            elif self.s.startswith("$(", i):
                i = self.substitution(i)
                out.append("$SUBST")
            elif c == "`" and not self.win:
                i = self.backtick(i)
                out.append("$SUBST")
            else:
                out.append(c)
                i += 1
        if closing or i > end:
            raise Unparsable("a double-quoted string is not closed" if closing
                             else "a substitution runs past the end of its here-document")
        return "".join(out), i

    def single_quoted(self, i: int) -> int:
        """At an opening single quote: return the index after the closing one."""
        s, j = self.s, i + 1
        ansi_c = not self.win and i > 0 and s[i - 1] == "$"  # $'...' takes backslash escapes
        while j < self.n:
            if ansi_c and s[j] == "\\":
                j += 2
            elif s[j] == "'":
                if self.win and s.startswith("''", j):  # PowerShell: '' is a quote
                    j += 2
                else:
                    return j + 1
            else:
                j += 1
        raise Unparsable("a single-quoted string is not closed")

    def herestring(self, i: int, out: list[str]) -> int | None:
        """At the quote of a PowerShell ``@'`` or ``@"``: skip the here-string and return the
        index after its closing line; None if this is not a here-string."""
        s, q = self.s, self.s[i]
        if not (self.win and out and out[-1] == "@" and re.match(r"[ \t]*\r?\n", s[i + 1:])):
            return None
        start = s.index("\n", i) + 1
        close = re.compile(rf"^{re.escape(q)}@", re.M).search(s, start)
        if not close:
            raise Unparsable(f"the PowerShell here-string @{q} has no closing line {q}@")
        if q == '"':
            self.expanded(start, close.start())
        out[-1] = " HERESTRING "
        return close.end()

    def heredoc(self, i: int, pending: list[tuple[str, bool, bool]], out: list[str]) -> int:
        """At a POSIX '<<' (not '<<<'): read the delimiter word (the body is read at the end of
        the line) and return the index after it."""
        s, j = self.s, i + 2
        strip_tabs = s.startswith("-", j)
        j += strip_tabs
        while j < self.n and s[j] in " \t":
            j += 1
        word: list[str] = []
        k, quote = j, ""
        while k < self.n and (quote or s[k] not in _WORD_END):
            ch = s[k]
            if quote:
                if ch == quote:
                    quote = ""
                else:
                    word.append(ch)
            elif ch in "'\"":
                quote = ch
            elif ch == "\\" and k + 1 < self.n:
                k += 1
                word.append(s[k])
            else:
                word.append(ch)
            k += 1
        delimiter = "".join(word)
        if quote or not delimiter or "\n" in delimiter:
            raise Unparsable("a here-document has no delimiter word")
        expanded = not any(q in s[j:k] for q in "'\"\\")
        pending.append((delimiter, strip_tabs, expanded))
        out.append(f" << {shlex.quote(delimiter)} ")
        return k

    def bodies(self, i: int, pending: list[tuple[str, bool, bool]]) -> int:
        """At the start of the line after here-document operators: skip their bodies, each up to
        its closing line, and return the index after the last one."""
        s = self.s
        for delimiter, strip_tabs, expanded in pending:
            start = i
            while True:
                if i >= self.n:
                    if not self.lenient:
                        raise Unparsable(f"the here-document '{delimiter}' has no closing line")
                    if expanded:
                        self.expanded(start, self.n)
                    break
                eol = s.find("\n", i)
                eol = self.n if eol < 0 else eol
                line = s[i:eol].rstrip("\r")
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    if expanded:
                        self.expanded(start, i)
                    i = eol + 1
                    break
                i = eol + 1
        pending.clear()
        return min(i, self.n)

    def scan(self, i: int = 0, until_paren: bool = False) -> tuple[str, int]:
        """The command text from ``i`` with bodies, here-strings and comments removed, substitutions
        replaced by ``$SUBST`` and newlines by ';'. With ``until_paren``, stop at the ')' closing
        a '$(' and return its index."""
        s, out = self.s, []
        pending: list[tuple[str, bool, bool]] = []
        depth = 0
        while i < self.n:
            c = s[i]
            if c == self.esc:
                if s.startswith("\n", i + 1) or s.startswith("\r\n", i + 1):
                    i += 2 if s[i + 1] == "\n" else 3  # line continuation
                else:
                    out.append(s[i:i + 2])
                    i += 2
            elif c in "'\"":
                j = self.herestring(i, out)
                if j is None and c == "'":
                    j = self.single_quoted(i)
                    out.append(s[i:j])
                elif j is None:
                    text, j = self.expanded(i + 1, self.n, closing='"')
                    out.append(f'"{text}"')
                i = j
            elif s.startswith("$(", i):
                i = self.substitution(i)
                out.append(" $SUBST ")
            elif c == "`" and not self.win:
                i = self.backtick(i)
                out.append(" $SUBST ")
            elif c == "#" and (i == 0 or s[i - 1] in _COMMENT_AFTER):
                while i < self.n and s[i] != "\n":
                    i += 1
            elif not self.win and s.startswith("<<<", i):
                out.append("<<<")
                i += 3
            elif not self.win and s.startswith("<<", i):
                i = self.heredoc(i, pending, out)
            elif c == "\n":
                out.append(" ; ")
                i = self.bodies(i + 1, pending)
            else:
                if c == "(":
                    depth += 1
                elif c == ")":
                    if until_paren and depth == 0:
                        if pending:
                            raise Unparsable(f"the here-document '{pending[0][0]}' has no body")
                        return "".join(out), i
                    depth = max(depth - 1, 0)
                out.append(c)
                i += 1
        if until_paren:
            raise Unparsable("a '$(' substitution is not closed")
        if pending and not self.lenient:
            raise Unparsable(f"the here-document '{pending[0][0]}' has no body and no closing line")
        return "".join(out), i


def strip_heredocs(command: str, windows_syntax: bool = False,
                   lenient: bool = False) -> tuple[str, list[str]]:
    """The command as the guard analyses it, and the commands substituted inside it.

    Here-document bodies (``<<WORD``, ``<<-WORD``) and PowerShell here-strings (``@'...'@``,
    ``@"..."@``) are removed: their text is data, never a redirection. When the shell expands
    them (unquoted delimiter, ``@"``), the commands they substitute (``$(...)``, backticks) are
    returned to be checked, as are those in double-quoted strings and in the command itself. A
    quoted delimiter (``<<'EOF'``, ``<<"EOF"``, ``<<\\EOF``) makes the body plain text. The
    redirection of the here-document (``cat > file <<'EOF'``) stays in the command. Comments are
    dropped and newlines outside quotes become ';' (they separate commands).

    Raises Unparsable when a body has no closing line (unless ``lenient``: then it runs to the end,
    as bash reads it), or a quote or a substitution is not closed.
    """
    scanner = _Scanner(command, windows_syntax, lenient)
    text, _ = scanner.scan()
    return text, scanner.nested


def tokenize(command: str, windows_syntax: bool = False) -> list[str]:
    """Split a shell command into words and operator tokens.

    ``windows_syntax`` (PowerShell, cmd): the backslash is a path separator, not an
    escape character, so it is turned into '/' before POSIX tokenisation.
    Braces opening or closing a script block or command group become separators.
    Comments are not recognised here (strip_heredocs drops them): shlex would also take a '#'
    inside a word for one and drop the rest of the line.
    """
    if windows_syntax:
        command = command.replace("\\", "/")
    command = re.sub(r"\d*>&\d+", " ", command)  # drop fd duplication such as 2>&1
    command = command.replace("&>", " > ")
    command = re.sub(r"(^|[\s;|&(])\{", r"\1 { ", command)
    command = re.sub(r"\}(?=$|[\s;|&)])", " } ", command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def split_segments(tokens: list[str]) -> list[list[str]]:
    """Split tokens into simple commands at shell separators."""
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _option_value(arg: str) -> str | None:
    """Value glued to an option: '--target-directory=x' or PowerShell '-Path:x'."""
    if arg.startswith("-"):
        for sep in ("=", ":"):
            if sep in arg:
                return arg.split(sep, 1)[1] or None
    return None


def analyse_segment(
    seg: list[str], windows_syntax: bool
) -> tuple[str, list[str], list[str], list[tuple[str, bool]], str]:
    """Analyse one simple command.

    Returns
    -------
    (name, arguments, write targets, nested commands, opaque reason)
        Nested commands are ``(command, windows_syntax)`` pairs to analyse
        recursively. A non-empty opaque reason means the effect of the command
        cannot be analysed and it must be denied to restricted roles.
    """
    args: list[str] = []
    targets: list[str] = []
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok in REDIRECTS_OUT and i + 1 < len(seg):
            targets.append(seg[i + 1])
            i += 2
            continue
        if tok in REDIRECTS_IN and i + 1 < len(seg):
            i += 2
            continue
        args.append(tok)
        i += 1
    while args and re.match(r"^[A-Za-z_]\w*=", args[0]):
        args = args[1:]
    while args and os.path.basename(args[0]) in WRAPPERS:
        args = args[1:]
        while args and (args[0].startswith("-") or re.match(r"^[A-Za-z_]\w*=", args[0])):
            args = args[1:]
    if not args:
        return "", [], targets, [], ""
    name = os.path.basename(args[0].replace("\\", "/")).lower().removesuffix(".exe")
    rest = args[1:]
    operands = [a for a in rest if not a.startswith("-")]
    glued = [v for v in map(_option_value, rest) if v]
    nested: list[tuple[str, bool]] = []
    opaque = ""
    lowered = [a.lower() for a in rest]

    if name in OPAQUE_COMMANDS:
        opaque = f"'{name}' runs code whose effect the guard cannot check"
    elif name in WRITE_ALL_ARGS:
        targets += operands + glued
        if not operands and not glued and name != "tee":
            opaque = f"'{name}' without an explicit path (e.g. pipeline input) cannot be checked"
    elif name in WRITE_LAST_ARG:
        if operands:
            targets.append(operands[-1])
        targets += glued
        for i, a in enumerate(lowered[:-1]):
            if a in {"-t", "--target-directory"}:
                targets.append(rest[i + 1])
    elif name in {"mkdir", "md"}:
        targets += [op.rstrip("/\\") + "/__probe__.py" for op in operands]
    elif name in {"sed", "perl"} and any(a.startswith("-i") for a in rest):
        targets += operands[1:] if name == "sed" else operands
    elif name == "ruff" and ("format" in rest or "--fix" in rest):
        paths = [a for a in operands if a not in {"check", "format"}]
        targets += paths or ["."]
    elif name in POSIX_SHELLS and "-c" in rest:
        idx = rest.index("-c")
        if idx + 1 < len(rest):
            nested.append((rest[idx + 1], False))
    elif name in POWERSHELLS:
        if any(a in {"-e", "-ec"} or (len(a) >= 3 and "-encodedcommand".startswith(a)) for a in lowered):
            opaque = "encoded PowerShell commands cannot be checked"
        idx = next((i for i, a in enumerate(lowered) if len(a) >= 2 and "-command".startswith(a)), None)
        inner = rest[idx + 1:] if idx is not None else operands
        nested.append((" ".join(inner), True))
    elif name == "cmd":
        idx = next((i for i, a in enumerate(lowered) if a.lstrip("/") in {"c", "k", "r"}
                    and a.startswith("/")), None)
        if idx is not None:
            nested.append((" ".join(rest[idx + 1:]), True))
    elif name == "find" and any(a in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
                                      "-fprint0", "-fprintf", "-fls"} for a in lowered):
        paths = []
        for a in rest:
            if a.startswith("-") or a in {"!", "("}:
                break
            paths.append(a)
        targets += paths or ["."]
    return name, rest, targets, nested, opaque


GREP_LIKE = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "select-string", "sls", "findstr"}


def is_recursive_read(name: str, rest: list[str]) -> bool:
    """True if the command reads every file under its directory operands."""
    if name in RECURSIVE_ALWAYS or name == "find":
        return True
    if name not in RECURSIVE_WITH_FLAG:
        return False
    for arg in (a.lower() for a in rest):
        if arg in RECURSIVE_FLAGS:
            return True
        # combined short flags of grep, e.g. -rn, -inr
        if name in {"grep", "egrep", "fgrep"} and re.fullmatch(r"-[a-z]*r[a-z]*", arg):
            return True
    return False


def path_operands(name: str, rest: list[str]) -> list[str]:
    """Operands of a command that are paths (for grep-like commands, all but the pattern)."""
    operands = [a for a in rest if not a.startswith("-")]
    return operands[1:] if name in GREP_LIKE else operands


def path_forms(token: str, windows_syntax: bool) -> set[str]:
    """Paths a shell word may denote: literal and with environment variables expanded.

    In Git Bash (POSIX syntax on Windows) /c/... and /tmp/... are translated to the
    Windows paths the shell actually opens.
    """
    def expand(m: re.Match[str]) -> str:
        var = next(g for g in m.groups() if g)
        return os.environ.get(var, m.group(0))

    forms = {token, ENV_REF.sub(expand, token)}
    if windows_syntax:
        forms = {f.replace("\\", "/") for f in forms}
    else:
        forms = {pathrules.msys_to_windows(f) or f for f in forms}
    return forms


def write_target_allowed(role: str, target: str, cwd: str | None) -> tuple[bool, str]:
    """Check a shell write target; directories are probed with a dummy child path."""
    rel = pathrules.to_repo_relative(target, PROJECT_DIR, cwd)
    if rel is not None:
        abs_target = PROJECT_DIR / rel if rel != "." else PROJECT_DIR
        if abs_target.is_dir():
            ok, _ = pathrules.check_write(role, str(abs_target / "__probe__.py"), PROJECT_DIR, cwd)
            if ok:
                return True, ""
            return False, (
                f"directory '{rel}' contains files outside the write allowlist of role "
                f"'{role}'. Pass explicit paths you own instead of a directory."
            )
    return pathrules.check_write(role, target, PROJECT_DIR, cwd)


def check_bash(role: str, command: str, cwd: str | None, depth: int = 0,
               windows_syntax: bool = False) -> None:
    """Best-effort check of a shell command against the role rules.

    ``windows_syntax`` is True for the PowerShell tool and for commands nested in
    ``powershell -Command`` or ``cmd /c``.
    """
    name_role, cfg = pathrules.role_config(PROJECT_DIR, role)
    if cfg is None:
        deny(f"Unknown role '{name_role}'. Check harness/roles.json.")
    if name_role == pathrules.OWNER_ROLE:
        check_owner_shell(command, cwd, depth, windows_syntax)
        return

    bash_exact = cfg.get("bash_exact")
    if bash_exact is not None and depth == 0:
        if not any(re.fullmatch(rx, command.strip()) for rx in bash_exact):
            deny(
                f"Role '{name_role}' may run only these exact commands (no cd, no pipes, "
                "no redirection): python harness/step_next.py | python harness/step_verify.py | "
                "python harness/step_record.py | python harness/step_resolve.py | git status | "
                "git log --oneline -N."
            )
        return

    # Here-document bodies, here-strings and comments are data: only the command text is analysed,
    # and the commands substituted anywhere in it are checked on their own.
    try:
        command, substituted = strip_heredocs(command, windows_syntax)
    except Unparsable as exc:
        deny(f"Shell command blocked: {exc}, so the guard cannot tell which text is a command. "
             "Write files with the Write tool." + FIX_HINT)
    for inner in substituted:
        if depth >= 3:
            deny("Shell commands nested more than three levels cannot be checked." + FIX_HINT)
        check_bash(role, inner, cwd, depth + 1, windows_syntax)

    # The step scripts belong to the orchestrator and the owner tools to the owner; a loop role
    # may only read the status and the approvals, or lint the task list.
    for m in HARNESS_SCRIPT.finditer(command):
        script, args = m.group(1), (m.group(2) or "").split()
        if script != "owner_tools" or (args and args[0] not in ("status", "lint", "approvals")):
            deny(f"Role '{name_role}' may not run harness/{script}.py"
                 f"{' ' + args[0] if args and script == 'owner_tools' else ''}: the loop "
                 "scripts belong to the orchestrator and the owner tools to the owner." + FIX_HINT)

    deny_read = cfg.get("deny_read", [])
    for literal in pathrules.literal_basenames(deny_read):
        if literal.lower() in command.lower():
            deny(
                f"Role '{name_role}' may not access files named '{literal}' "
                "(independence rule)." + FIX_HINT
            )

    if DOTNET_FILE_API.search(command):
        deny(".NET file APIs in shell commands cannot be checked by the guard; use the "
             "Write and Edit tools." + FIX_HINT)

    tokens = tokenize(command, windows_syntax)
    bash_allow = cfg.get("bash_allow")
    for seg in split_segments(tokens):
        name, rest, targets, nested, opaque = analyse_segment(seg, windows_syntax)
        if not name and not targets:
            continue
        if bash_allow is not None and name and name not in bash_allow:
            deny(
                f"Role '{name_role}' may only run these shell commands: "
                f"{', '.join(bash_allow)}. '{name}' is not allowed." + FIX_HINT
            )
        if opaque:
            deny(f"Shell command blocked: {opaque}." + FIX_HINT)
        if name == "git" and rest:
            i = 0
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in GIT_OPTS_WITH_VALUE else 1
            sub = rest[i] if i < len(rest) else ""
            if sub in GIT_FORBIDDEN:
                deny(f"'git {sub}' is reserved to the owner." + FIX_HINT)
            git_allow = cfg.get("git_allow")
            if git_allow is not None and sub not in git_allow:
                deny(f"Role '{name_role}' may only use: git {', git '.join(git_allow)}." + FIX_HINT)
            if sub in {"rm", "mv"}:
                targets += [a for a in rest[i + 1:] if not a.startswith("-")]
            sub_args = rest[i + 1:]
            # The owner may be editing other files in the same working tree: a loop role stages
            # only the files it changed.
            if sub == "add" and any(a in {"-A", "--all", ".", "-u", "--update", ":/", "*"} or
                                    a.startswith(":/") for a in sub_args):
                deny("Stage only the files you changed: 'git add <path> ...', never 'git add -A', "
                     "'git add .' or 'git add -u' (the owner may have edits of their own in this "
                     "working tree)." + FIX_HINT)
            if sub == "commit" and any(a == "--all" or re.fullmatch(r"-[A-Za-z]*a[A-Za-z]*", a)
                                       for a in sub_args):
                deny("Do not commit with -a/--all: stage the files you changed with 'git add <path>' "
                     "(the owner may have edits of their own in this working tree)." + FIX_HINT)
        for target in targets:
            if target.lower() in NULL_DEVICES:
                continue
            for form in path_forms(target, windows_syntax):
                ok, why = write_target_allowed(name_role, form, cwd)
                if not ok:
                    deny(f"Shell write blocked: {why}" + FIX_HINT)
        if deny_read:
            recursive = is_recursive_read(name, rest)
            for tok in seg:
                for form in path_forms(tok, windows_syntax):
                    for candidate in {form, form.split(":", 1)[-1]}:
                        ok, why = pathrules.check_read(name_role, candidate, PROJECT_DIR, cwd,
                                                       recursive=recursive)
                        if not ok:
                            deny(why)
            if recursive and not path_operands(name, rest):
                # no path operand: the command searches the current directory
                ok, why = pathrules.check_read(name_role, cwd or str(PROJECT_DIR), PROJECT_DIR, cwd,
                                               recursive=True)
                if not ok:
                    deny(why)
        for inner, inner_windows in nested:
            if depth >= 3:
                deny("Shell commands nested more than three levels cannot be checked." + FIX_HINT)
            check_bash(role, inner, cwd, depth + 1, inner_windows)


def check_owner_shell(command: str, cwd: str | None, depth: int, windows_syntax: bool) -> None:
    """The owner is unrestricted except for writes into the protected paths of the project."""
    if cwd and pathrules.in_protected_path(cwd, PROJECT_DIR):
        lowered = command.lower()
        if any(w in lowered for w in (" > ", ">>", "git commit", "git add", "rm ", "mv ", "pip ")):
            deny("Shell command blocked: it would write inside a protected path, which is "
                 "read-only for every role (harness/project.json, protected_paths).")
    # A here-document whose closing line is not exact (indented, trailing blank) runs to the end
    # of the command, as bash reads it: its body and delimiter are not paths the owner writes.
    try:
        command, substituted = strip_heredocs(command, windows_syntax, lenient=True)
    except Unparsable:
        # an unclosed quote or substitution: the raw text, a command per line
        command, substituted = command.replace("\n", " ; "), []
    for inner in substituted:
        if depth >= 3:
            deny("Shell commands nested more than three levels cannot be checked.")
        check_owner_shell(inner, cwd, depth + 1, windows_syntax)
    for seg in split_segments(tokenize(command, windows_syntax)):
        name, rest, targets, nested, _opaque = analyse_segment(seg, windows_syntax)
        if name == "git" and "-C" in rest:
            i = rest.index("-C")
            if i + 1 < len(rest) and pathrules.in_protected_path(rest[i + 1], PROJECT_DIR, cwd):
                sub = next((a for a in rest[i + 2:] if not a.startswith("-")), "")
                if sub not in {"status", "log", "diff", "show", "rev-parse", "ls-files", "branch",
                               "grep", "blame"}:
                    deny(f"'git -C <protected path> {sub}' blocked: protected paths are read-only.")
        for target in targets:
            for form in path_forms(target, windows_syntax):
                if pathrules.in_protected_path(form, PROJECT_DIR, cwd):
                    deny(f"Shell write blocked: '{target}' is inside a protected path, read-only "
                         "for every role (harness/project.json, protected_paths).")
        record_owner_writes([t for t in targets if t.lower() not in NULL_DEVICES], cwd)
        for inner, inner_windows in nested:
            if depth >= 3:
                deny("Shell commands nested more than three levels cannot be checked.")
            check_owner_shell(inner, cwd, depth + 1, inner_windows)


def bind_orchestrator(session_id: str | None) -> None:
    """Bind this session as the loop orchestrator (it is about to run step_next.py)."""
    if not session_id:
        deny("The hook input has no session_id: the loop cannot bind its orchestrator. Tell the owner.")

    def change(state: dict) -> None:
        bound = state.get("orchestrator_session")
        if bound and bound != session_id and state.get("loop_active"):
            raise PermissionError
        state["orchestrator_session"] = session_id

    try:
        statefile.update(STATE_FILE, change)
    except PermissionError:
        deny("The loop is already running in another session. Use that session, or stop it "
             "(AGENT_SOFT_STOP / AGENT_STOP) and run 'python harness/owner_tools.py' with ACTION "
             "'release' before starting it here.")
    except (OSError, ValueError, statefile.LockBusy) as exc:
        deny(f"harness/state.json cannot be updated ({exc}); failing closed. Tell the owner.")


def write_heartbeat(event: dict) -> None:
    """Record that the hook ran, and for which command; the step scripts refuse to run unless
    the heartbeat shows the guard intercepted the very shell call that started them."""
    tool_input = event.get("tool_input") or {}
    if SELFTEST:
        return
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(json.dumps({
            "time": time.time(),
            "tool": event.get("tool_name", ""),
            "command": (tool_input.get("command") or "").strip()[:300],
            "agent_type": event.get("agent_type"),
            "session_id": event.get("session_id"),
        }), encoding="utf-8")
    except OSError:
        pass


def check_dispatch(tool_input: dict) -> None:
    """The orchestrator may spawn only the subagent the step scripts expect, with their prompt.

    The expected spawn is ``state.current.spawn``: ``{"agent", "prompt_file", "token_line"}``,
    written by the step script that asked for it (dispatch, review, resolution or challenge).
    """
    try:
        state = pathrules.read_state(PROJECT_DIR)
    except ValueError:
        state = {}
    spawn = (state.get("current") or {}).get("spawn")
    if not spawn:
        deny("No subagent is expected now. Run: python harness/step_next.py")
    expected, prompt_file = spawn["agent"], spawn["prompt_file"]
    requested = tool_input.get("subagent_type")
    if requested != expected:
        deny(f"Spawn the '{expected}' subagent now (subagent_type='{expected}'), not "
             f"'{requested}'. Its prompt is the full content of {prompt_file}.")
    # Absent when background tasks are disabled (CLAUDE_CODE_DISABLE_BACKGROUND_TASKS): then the
    # Agent tool always runs in the foreground. Only an explicit request for background is refused.
    if tool_input.get("run_in_background") is True:
        deny("Spawn the subagent in the foreground (run_in_background=false or omitted), so that you "
             "wait for it to return before running the next step script.")
    if spawn["token_line"] not in (tool_input.get("prompt") or ""):
        deny(f"The prompt must be the full content of {prompt_file}: read it and pass it verbatim.")


def check_stop(role: str, tool: str, tool_input: dict, cwd: str | None) -> None:
    """AGENT_STOP is present: let a loop role do only what winds it down (see the module doc).

    The step scripts read the denials in harness/logs/guard_blocks.jsonl (messages starting with
    STOP_PREFIX) to tell a session the owner stopped from one that failed."""
    if role == pathrules.ORCHESTRATOR_ROLE:
        if tool in AGENT_TOOLS:
            deny(f"{STOP_PREFIX}: the owner is stopping the loop. Spawn no subagent: run python "
                 "harness/step_next.py and end your turn when it prints STOPPED.")
        return
    if role not in pathrules.dispatch_roles(PROJECT_DIR):
        deny(f"{STOP_PREFIX}: the owner is stopping the loop. End your turn now, without writing "
             "your output file: the harness spawns you again when the loop restarts.")
    if tool in READ_TOOLS:
        return
    if tool in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if pathrules.to_repo_relative(path, PROJECT_DIR, cwd) == "harness/claim.json":
            return
    elif tool in SHELL_TOOLS and stop_shell_allowed(tool_input.get("command") or "",
                                                    tool == "PowerShell"):
        return
    deny(STOP_TASK_MESSAGE)


def stop_shell_allowed(command: str, windows_syntax: bool, depth: int = 0) -> bool:
    """Only git commands that commit or inspect, with a commit message from a substitution
    (``git commit -m "$(cat <<'EOF' ...)"``) allowed; the usual checks still follow."""
    try:
        text, nested = strip_heredocs(command, windows_syntax)
        segments = split_segments(tokenize(text, windows_syntax))
    except ValueError:
        return False
    for seg in segments:
        if any(tok in REDIRECTS_OUT for tok in seg):
            return False
        if git_subcommand(seg) in STOP_GIT:
            continue
        if not (depth and os.path.basename(seg[0]).lower() in STOP_MESSAGE_COMMANDS):
            return False
    return bool(segments) and depth < 3 and all(
        stop_shell_allowed(inner, windows_syntax, depth + 1) for inner in nested)


def git_subcommand(seg: list[str]) -> str | None:
    """The subcommand of a git command segment (``git -C x commit`` -> commit), else None."""
    if not seg or os.path.basename(seg[0]).lower() not in ("git", "git.exe"):
        return None
    args = iter(seg[1:])
    for arg in args:
        if arg in GIT_OPTS_WITH_VALUE:
            next(args, None)
        elif not arg.startswith("-"):
            return arg
    return None


def record_owner_writes(paths: list[str], cwd: str | None) -> None:
    """Record the repository files the owner writes while the loop is active (harness/
    owner_writes.json): the owner may work in a second session on the same working tree, and the
    step scripts must not take those edits for the task's work. Never blocks the owner."""
    try:
        state = pathrules.read_state(PROJECT_DIR)
        if not (state.get("current") or state.get("loop_active")):
            return
        rels = set()
        for p in paths:
            rel = pathrules.to_repo_relative(p, PROJECT_DIR, cwd)
            if rel and rel != "." and not rel.endswith("__probe__.py"):
                rels.add(rel)
        if rels:
            statefile.update(OWNER_WRITES, lambda d: d.update(
                paths=sorted(set(d.get("paths", [])) | rels)))
    except Exception:  # noqa: BLE001 - recording must never block the owner
        pass


def check_search(role: str, tool: str, tool_input: dict, cwd: str | None) -> None:
    """Grep reads every file under its path; Glob lists names. Both are read-checked."""
    path = tool_input.get("path") or cwd or str(PROJECT_DIR)
    ok, why = pathrules.check_read(role, path, PROJECT_DIR, cwd, recursive=tool == "Grep")
    if not ok:
        deny(why)


def run() -> None:
    """Check one hook event; returns normally when the call may proceed."""
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:  # includes UnicodeDecodeError and JSONDecodeError
        deny("Guard could not parse the hook input; failing closed. Tell the owner.")
    if not isinstance(event, dict):
        deny("Guard received a hook input that is not a JSON object; failing closed. Tell the owner.")

    write_heartbeat(event)

    if (PROJECT_DIR / "AGENT_EMERGENCY_STOP").exists():
        deny("AGENT_EMERGENCY_STOP is present: the owner has halted all agent activity. "
             "Stop working now and end your turn without further tool calls.")

    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd")
    session_id = event.get("session_id")
    if (tool in SHELL_TOOLS and not event.get("agent_type")
            and STEP_NEXT.fullmatch((tool_input.get("command") or "").strip())):
        bind_orchestrator(session_id)
    try:
        pathrules.read_state(PROJECT_DIR)  # an unreadable state blocks every role (fail closed)
        role, role_source = pathrules.role_resolution(PROJECT_DIR, event.get("agent_type"),
                                                      session_id, event.get("agent_id"))
    except ValueError as exc:
        deny(f"{exc} Failing closed.")
    CALL.update(role=role, role_source=role_source, agent_type=event.get("agent_type"), tool=tool,
                target=(tool_input.get("file_path") or tool_input.get("notebook_path")
                        or tool_input.get("command") or "")[:300])
    try:
        CALL.update(call_details(event, tool, tool_input, cwd))
    except Exception:  # noqa: BLE001 - the details are for the log only, never block on them
        pass
    if (PROJECT_DIR / "AGENT_STOP").exists() and role != pathrules.OWNER_ROLE:
        check_stop(role, tool, tool_input, cwd)

    if tool in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if path:
            ok, why = pathrules.check_write(role, path, PROJECT_DIR, cwd)
            if not ok:
                deny(f"Write blocked: {why}" + FIX_HINT)
            if role == pathrules.OWNER_ROLE:
                record_owner_writes([path], cwd)
    elif tool == "Read":
        path = tool_input.get("file_path")
        if path:
            ok, why = pathrules.check_read(role, path, PROJECT_DIR, cwd)
            if not ok:
                deny(why)
    elif tool in SEARCH_TOOLS:
        check_search(role, tool, tool_input, cwd)
    elif tool in SHELL_TOOLS:
        check_bash(role, tool_input.get("command") or "", cwd,
                   windows_syntax=tool == "PowerShell")
    elif tool in AGENT_TOOLS and role == pathrules.ORCHESTRATOR_ROLE:
        check_dispatch(tool_input)


def main() -> None:
    """Run the checks; any unexpected error blocks the call (exit 2), never lets it through."""
    try:
        run()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - fail closed on anything
        deny(f"Guard internal error ({type(exc).__name__}: {exc}); failing closed. Tell the owner.")
    sys.exit(0)


if __name__ == "__main__":
    main()
