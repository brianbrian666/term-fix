"""Command line entry point.

Design notes that matter for everyday use:

* everything term-fix prints goes to **stderr**, so ``tf npm run build > log``
  still writes a clean log file;
* term-fix's own flags only count *before* the wrapped command, so
  ``tf apt install -y foo`` forwards ``-y`` to apt, not to us;
* the exit code of the wrapped command (or of the fix) is what you get back.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__, ai, context, executor, history, safety
from .config import CONFIG_PATH, HISTORY_PATH, Settings, load_settings

console = Console(stderr=True, highlight=False)

# Flags that consume the next token, used by the pre-parser.
_VALUE_OPTIONS = {"--provider", "--model", "--base-url", "--lang", "--max-chars", "--timeout", "--limit"}

EPILOG = """\b
Examples:
  tf npm install                       # run it, and debug it if it fails
  tf --yes npm run build               # auto-approve safe fixes
  tf --dry-run mvn -q package          # explain only, never execute
  tf --capture-all pytest -x           # also capture stdout
  tf --model gemini-2.5-pro docker ps  # pick another model
  tf --doctor --check                  # verify keys and connectivity

term-fix's own flags must come before the command. Everything from the first
non-flag word onwards is passed to the command untouched.
"""


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``["-y", "npm", "i", "--force"]`` into tf flags and the command."""
    head: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return head, argv[index + 1:]
        if not token.startswith("-") or token == "-":
            return head, argv[index:]
        name, separator, _ = token.partition("=")
        head.append(token)
        if name in _VALUE_OPTIONS and not separator and index + 1 < len(argv):
            index += 1
            head.append(argv[index])
        index += 1
    return head, []


def _short(text: str, width: int = 46) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def render_diagnosis(diagnosis: ai.Diagnosis, assessment: safety.Assessment) -> None:
    colour = {safety.Risk.LOW: "green", safety.Risk.MEDIUM: "yellow"}.get(assessment.risk, "red")
    body = Text()
    body.append("cause  ", style="bold")
    body.append(diagnosis.explanation or "(no explanation given)", style="yellow")
    body.append("\n")
    if diagnosis.has_fix:
        body.append("fix    ", style="bold")
        body.append(diagnosis.fix_command, style="bold cyan")
    else:
        body.append("fix    ", style="bold")
        body.append("(no safe automatic fix)", style="dim")
    if diagnosis.notes:
        body.append("\n\n")
        body.append(diagnosis.notes, style="dim")

    subtitle = f"{diagnosis.provider}/{diagnosis.model} · risk {assessment.risk.label} · confidence {diagnosis.confidence}"
    console.print()
    console.print(Panel(body, title="term-fix", subtitle=subtitle, border_style=colour, padding=(0, 1)))


def print_setup_help(message: str) -> None:
    body = Text()
    body.append(message + "\n\n", style="yellow")
    body.append("Pick one:\n", style="bold")
    body.append("  export GEMINI_API_KEY=...        # https://aistudio.google.com/apikey\n")
    body.append("  export OPENAI_API_KEY=...        # or any OpenAI-compatible gateway\n")
    body.append("  export TERM_FIX_BASE_URL=http://localhost:11434/v1   # local Ollama\n\n")
    body.append(f"Or write it down once in {CONFIG_PATH}:\n", style="bold")
    body.append('  provider = "gemini"\n  model = "gemini-2.5-flash"\n  lang = "zh"\n')
    console.print(Panel(body, title="term-fix setup", border_style="yellow", padding=(0, 1)))


def ask_to_run(assessment: safety.Assessment, *, assume_yes: bool, force: bool) -> bool:
    """Decide whether the generated command may actually run."""
    interactive = sys.stdin.isatty()

    if assessment.risk is safety.Risk.CRITICAL:
        reason = ", ".join(assessment.reasons) or "destructive"
        if not force:
            console.print(Text(f"refused: {reason}. Re-run with --force if you really mean it.", style="bold red"))
            return False
        if not interactive:
            console.print(Text("refused: a critical-risk command needs an interactive confirmation.", style="bold red"))
            return False
        return click.confirm("This command can destroy data. Run it anyway?", default=False, err=True)

    if assessment.is_dangerous:
        console.print(Text(f"warning: {', '.join(assessment.reasons)}", style="yellow"))

    if assume_yes and (not assessment.is_dangerous or force):
        return True

    if not interactive:
        console.print(Text("skipped: no tty for confirmation (use --yes, or copy the command above).", style="dim"))
        return False

    prompt = "Run the fix?" if assessment.risk < safety.Risk.HIGH else "Risky command — run it anyway?"
    return click.confirm(prompt, default=assessment.risk < safety.Risk.HIGH, err=True)


def run_flow(
    command: tuple[str, ...],
    *,
    assume_yes: bool,
    dry_run: bool,
    force: bool,
    capture_all: bool,
    quiet: bool,
    settings: Settings,
) -> int:
    tokens = list(command)
    display = " ".join(tokens) if executor.needs_shell(tokens) else shlex.join(tokens)

    if not quiet:
        console.print(Text(f"$ {display}", style="dim"))

    result = executor.run_capture(executor.build_invocation(tokens), capture_stdout=capture_all)

    if result.interrupted:
        console.print(Text("interrupted", style="dim"))
        return 130
    if result.timed_out:
        console.print(Text("command timed out", style="dim"))
        return 124
    if result.ok:
        return 0

    if not quiet:
        console.print(Text(f"exit {result.returncode} ({result.duration:.1f}s)", style="red"))

    stream = "stderr"
    error_text = result.stderr
    if not error_text.strip() and capture_all and result.stdout.strip():
        stream = "stdout"
        error_text = result.stdout
    if not error_text.strip():
        hint = Text("the command failed without printing anything.\n")
        if not capture_all:
            hint.append("some tools write errors to stdout — try again with --capture-all.", style="dim")
        console.print(Panel(hint, title="term-fix", border_style="yellow", padding=(0, 1)))
        return result.returncode

    tail, dropped = executor.tail_for_ai(error_text, settings.max_error_chars)

    try:
        with console.status(Text("analysing failure...", style="dim"), spinner="dots"):
            env = context.gather(display)
            diagnosis = ai.analyze_error(
                command=display,
                exit_code=result.returncode,
                stderr=safety.redact(tail),
                env=env,
                settings=settings,
                dropped_chars=dropped,
                stream=stream,
            )
    except ai.MissingCredentials as exc:
        print_setup_help(str(exc))
        return result.returncode
    except ai.AIError as exc:
        console.print(Panel(Text(str(exc), style="red"), title="term-fix: model error", border_style="red", padding=(0, 1)))
        return result.returncode

    assessment = safety.assess(diagnosis.fix_command) if diagnosis.has_fix else safety.Assessment()
    render_diagnosis(diagnosis, assessment)

    executed = False
    fix_code: int | None = None
    if diagnosis.has_fix and not dry_run:
        if ask_to_run(assessment, assume_yes=assume_yes, force=force):
            console.print(Text("running fix...", style="dim"))
            fix_code = executor.run_passthrough(executor.shell_argv(diagnosis.fix_command))
            executed = True
            if fix_code == 0:
                console.print(Text("fix finished (exit 0)", style="green"))
            else:
                console.print(Text(f"fix failed (exit {fix_code})", style="red"))

    history.record(
        command=display,
        exit_code=result.returncode,
        explanation=diagnosis.explanation,
        fix_command=diagnosis.fix_command,
        risk=assessment.risk.label,
        provider=diagnosis.provider,
        model=diagnosis.model,
        executed=executed,
        fix_exit_code=fix_code,
        cwd=str(Path.cwd()),
    )

    if fix_code is not None:
        return fix_code
    return result.returncode


def print_doctor(settings: Settings, check: bool) -> int:
    provider, model, provider_error = "", "", None
    try:
        provider, model = ai.resolve_provider(settings)
    except ai.AIError as exc:
        provider_error = str(exc)

    table = Table(box=None, show_header=False, padding=(0, 2), title="term-fix doctor", title_justify="left")
    table.add_column(style="bold", no_wrap=True)
    table.add_column(overflow="fold")

    table.add_row("version", f"{__version__} (python {sys.version.split()[0]})")
    table.add_row("platform", f"{sys.platform}")
    table.add_row("config", f"{CONFIG_PATH}{'' if CONFIG_PATH.exists() else '  (not created yet)'}")
    table.add_row("history", str(HISTORY_PATH))
    table.add_row("provider", provider or "(not configured)")
    table.add_row("model", model or "(default)")
    table.add_row("base url", settings.base_url or "(default endpoint)")
    table.add_row("language", settings.lang)
    table.add_row("GEMINI_API_KEY", safety.mask_secret(ai.gemini_key()))
    table.add_row("OPENAI_API_KEY", safety.mask_secret(ai.openai_key()))
    table.add_row("auto-yes", str(settings.auto_yes))
    console.print(table)

    if provider_error:
        print_setup_help(provider_error)
        return 1

    if not check:
        console.print(Text("run `tf --doctor --check` to make one live model call", style="dim"))
        return 0

    env = context.gather("true")
    try:
        with console.status(Text("calling the model...", style="dim"), spinner="dots"):
            diagnosis = ai.analyze_error(
                command="npm install",
                exit_code=1,
                stderr="npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree",
                env=env,
                settings=settings,
            )
    except ai.AIError as exc:
        console.print(Panel(Text(str(exc), style="red"), title="live check failed", border_style="red", padding=(0, 1)))
        return 1

    console.print(Panel(
        Text.assemble(
            (diagnosis.explanation, "yellow"), "\n",
            (diagnosis.fix_command or "(no command)", "cyan"),
        ),
        title=f"live check ok · {diagnosis.provider}/{diagnosis.model}",
        border_style="green",
        padding=(0, 1),
    ))
    return 0


def print_history(limit: int) -> int:
    entries = history.load(limit)
    if not entries:
        console.print(Text("no runs recorded yet.", style="dim"))
        return 0

    table = Table(title=f"last {len(entries)} runs", box=None, header_style="bold")
    table.add_column("when", no_wrap=True, style="dim")
    table.add_column("command", overflow="fold")
    table.add_column("risk", no_wrap=True)
    table.add_column("fix", overflow="fold")
    table.add_column("ran", no_wrap=True)

    for entry in entries:
        ran = "yes" if entry.get("executed") else "no"
        fix_code = entry.get("fix_exit_code")
        if entry.get("executed") and fix_code not in (None, 0):
            ran = f"exit {fix_code}"
        risk = str(entry.get("risk", ""))
        colour = {"low": "green", "medium": "yellow"}.get(risk, "red")
        table.add_row(
            str(entry.get("ts", "")),
            _short(str(entry.get("command", "")), 40),
            Text(risk, style=colour),
            _short(str(entry.get("fix_command") or "(none)"), 48),
            ran,
        )
    console.print(table)
    return 0


@click.command(
    context_settings={
        "help_option_names": ["-h", "--help"],
        "ignore_unknown_options": True,
        "max_content_width": 110,
    },
    epilog=EPILOG,
    no_args_is_help=True,
)
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
@click.option("-y", "--yes", "assume_yes", is_flag=True, help="Run the fix without asking (high-risk fixes still ask).")
@click.option("--dry-run", "dry_run", is_flag=True, help="Explain the failure but never execute the fix.")
@click.option("--force", is_flag=True, help="Allow high-risk fixes to run non-interactively.")
@click.option("--capture-all", is_flag=True, help="Also capture stdout (some tools print errors there).")
@click.option("-q", "--quiet", is_flag=True, help="Hide everything except the diagnosis panel.")
@click.option("--provider", type=click.Choice(["auto", "gemini", "openai"]), default=None,
              help="Model provider (default: auto-detect from your keys).")
@click.option("--model", default=None, help="Model name, e.g. gemini-2.5-flash.")
@click.option("--base-url", default=None, help="OpenAI-compatible endpoint, e.g. http://localhost:11434/v1")
@click.option("--lang", default=None, help="Language of the explanation (zh, en, ...).")
@click.option("--max-chars", type=int, default=None, help="How much of the error tail to send.")
@click.option("--timeout", type=float, default=None, help="Model request timeout in seconds.")
@click.option("--doctor", is_flag=True, help="Show config, keys and connectivity.")
@click.option("--check", is_flag=True, help="With --doctor: make one live model call.")
@click.option("--history", "show_history", is_flag=True, help="Show recently recorded runs.")
@click.option("--limit", type=int, default=10, help="Rows shown by --history.")
@click.version_option(__version__, "-V", "--version", prog_name="term-fix")
def cli(
    cmd: tuple[str, ...],
    assume_yes: bool,
    dry_run: bool,
    force: bool,
    capture_all: bool,
    quiet: bool,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    lang: str | None,
    max_chars: int | None,
    timeout: float | None,
    doctor: bool,
    check: bool,
    show_history: bool,
    limit: int,
) -> int:
    settings = load_settings(
        provider=provider,
        model=model,
        base_url=base_url,
        lang=lang,
        auto_yes=True if assume_yes else None,
        max_error_chars=max_chars,
        timeout=timeout,
    )
    assume_yes = assume_yes or settings.auto_yes

    if doctor or check:
        return print_doctor(settings, check)
    if show_history:
        return print_history(limit)
    if not cmd:
        click.echo(cli.get_help(click.get_current_context()), err=True)
        return 2

    return run_flow(
        cmd,
        assume_yes=assume_yes,
        dry_run=dry_run,
        force=force,
        capture_all=capture_all,
        quiet=quiet,
        settings=settings,
    )


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point: split tf's own flags, then run."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    head, command = split_argv(arguments)
    args = head + (["--"] + command if command else [])
    try:
        code = cli.main(args=args, prog_name="tf", standalone_mode=False)
    except click.exceptions.Exit as exc:  # raised by --help/--version
        code = exc.exit_code
    except click.ClickException as exc:
        exc.show()
        code = exc.exit_code
    except click.Abort:
        code = 130
    except KeyboardInterrupt:
        code = 130
    sys.exit(0 if code is None else int(code))


if __name__ == "__main__":  # pragma: no cover
    main()
