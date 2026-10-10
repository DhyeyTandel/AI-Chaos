import asyncio
import signal
import time

import typer
from rich.console import Console
from rich.table import Table

from . import docker_api
from .prober import JOURNEYS, run_probe
from .stats import journey_stats
from .store import Store

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.command()
def status():
    """Show all ai-chaos containers. Exit 1 if any is not running or unhealthy."""
    infos = docker_api.list_project_containers()
    table = Table("name", "state", "health", "restarts", "oom")
    bad = not infos
    for i in infos:
        problem = i.state != "running" or i.health == "unhealthy"
        bad = bad or problem
        style = "red" if problem else None
        table.add_row(i.name, i.state, i.health, str(i.restarts), "yes" if i.oom_killed else "no", style=style)
    console.print(table)
    if not infos:
        console.print("no containers found for project ai-chaos")
    raise typer.Exit(1 if bad else 0)


@app.command()
def probe(seconds: int = typer.Option(30, "--seconds", "-s", min=1)):
    """Run the journey prober for N seconds and print per-journey stats."""
    store = Store()
    t0 = time.time()

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, stop.set)
        except NotImplementedError:
            pass
        await run_probe(store, seconds, stop=stop)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("interrupted")

    table = Table("journey", "count", "success %", "p50 ms", "p95 ms")
    failed = False
    for j in JOURNEYS:
        s = journey_stats(store, j, since=t0)
        if s.count == 0 or s.success_rate < 1.0:
            failed = True
        table.add_row(
            j,
            str(s.count),
            "-" if s.success_rate is None else f"{s.success_rate * 100:.1f}",
            "-" if s.p50 is None else f"{s.p50:.0f}",
            "-" if s.p95 is None else f"{s.p95:.0f}",
        )
    console.print(table)
    store.close()
    raise typer.Exit(1 if failed else 0)
