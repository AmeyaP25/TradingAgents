import sys
from pathlib import Path

import typer

from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.client_profile import (
    apply_client_profile_overrides,
    extract_client_profile,
    load_client_profile,
    load_overrides,
    save_client_profile,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind."),
    port: int = typer.Option(8000, "--port", help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload for local development."),
):
    """Run a local HTTP API server."""
    try:
        import uvicorn
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]uvicorn is required to run the API server: {exc}[/red]")
        raise typer.Exit(code=1) from None
    uvicorn.run("tradingagents.local_api:create_app", factory=True, host=host, port=port, reload=reload)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    client_profile: str = typer.Option(
        None,
        "--client-profile",
        help="JSON file with a structured client profile.",
    ),
    client_case_study: str = typer.Option(
        None,
        "--client-case-study",
        help="Text file containing the client case study to parse into a structured profile.",
    ),
    client_profile_overrides: str = typer.Option(
        None,
        "--client-profile-overrides",
        help="JSON file with partial fields to override extracted or loaded client profile values.",
    ),
    save_client_profile_to: str = typer.Option(
        None,
        "--save-client-profile",
        help="Write the resolved client profile JSON to this path before analysis.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
    profile = None
    if client_profile and client_case_study:
        console.print("[red]Use either --client-profile or --client-case-study, not both.[/red]")
        raise typer.Exit(code=1)
    if client_profile:
        try:
            profile = load_client_profile(client_profile)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
    if client_case_study:
        try:
            case_text = Path(client_case_study).read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]Could not read case-study file: {exc}[/red]")
            raise typer.Exit(code=1) from None
        profile = extract_client_profile(case_text)
        console.print("\n[bold cyan]Extracted Client Profile (review before analysis):[/bold cyan]\n")
        console.print(profile.model_dump_json(indent=2))
        approved = typer.prompt(
            "Use this extracted profile? (Y to continue, anything else to cancel)",
            default="Y",
        ).strip().upper() in ("Y", "YES", "")
        if not approved:
            console.print("[yellow]Cancelled before analysis so you can edit the profile.[/yellow]")
            raise typer.Exit(code=0)
    if client_profile_overrides:
        if profile is None:
            console.print("[red]--client-profile-overrides requires --client-profile or --client-case-study.[/red]")
            raise typer.Exit(code=1)
        try:
            overrides = load_overrides(client_profile_overrides)
            profile = apply_client_profile_overrides(profile, overrides)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        console.print("\n[bold cyan]Client Profile after overrides:[/bold cyan]\n")
        console.print(profile.model_dump_json(indent=2))
        approved = typer.prompt(
            "Use this resolved profile? (Y to continue, anything else to cancel)",
            default="Y",
        ).strip().upper() in ("Y", "YES", "")
        if not approved:
            console.print("[yellow]Cancelled before analysis so you can edit overrides.[/yellow]")
            raise typer.Exit(code=0)
    if save_client_profile_to and profile is not None:
        try:
            save_client_profile(profile, save_client_profile_to)
        except OSError as exc:
            console.print(f"[red]Could not save client profile: {exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show}
        kwargs = {"checkpoint": checkpoint, "portfolio": portfolio_context, "flags": flags}
        if profile is not None:
            kwargs["client_profile"] = profile
        run_analysis(**kwargs)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    client_profile: str = typer.Option(
        None, "--client-profile", help="JSON file with structured client profile for every grid cell"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        profile = load_client_profile(client_profile) if client_profile else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "client_profile": profile, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


if __name__ == "__main__":
    app()
