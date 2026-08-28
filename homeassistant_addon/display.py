"""Rich terminal display for flight results."""

from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

console = Console()


def _price_color(price_value: float, threshold: float, lowest_ever) -> str:
    if threshold and price_value <= threshold:
        return "bold green"
    if lowest_ever and price_value <= lowest_ever:
        return "green"
    return "white"


def print_results(results: list[dict], config: dict) -> None:
    threshold = config.get("price_alert_threshold", 0)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    console.rule(f"[bold cyan]Flight Tracker — {now}[/bold cyan]")
    dep_before = config.get("departure_range_before", 3)
    dep_after = config.get("departure_range_after", 3)
    ret_before = config.get("return_range_before", 3)
    ret_after = config.get("return_range_after", 3)
    console.print(
        f"[dim]Route:[/dim] [bold]{config['origins']}[/bold] → [bold]{config['destination']}[/bold]  "
        f"[dim]Depart:[/dim] [bold]{config['ideal_date']} -{dep_before}/+{dep_after}d[/bold]  "
        f"[dim]Return:[/dim] [bold]{config['ideal_return_date']} -{ret_before}/+{ret_after}d[/bold]"
    )

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    # Group by origin
    by_origin: dict[str, list[dict]] = {}
    for r in results:
        by_origin.setdefault(r["origin"], []).append(r)

    for origin, flights in by_origin.items():
        table = Table(
            title=f"[bold]{origin} → {config['destination']}[/bold]",
            box=box.ROUNDED,
            show_lines=True,
            header_style="bold magenta",
        )
        table.add_column("Depart date", style="cyan", no_wrap=True)
        table.add_column("Return date", style="cyan", no_wrap=True)
        table.add_column("Airline", style="white")
        table.add_column("Departure", style="dim")
        table.add_column("Arrival", style="dim")
        table.add_column("Duration", style="dim")
        table.add_column("Stops", justify="center")
        table.add_column("Price", justify="right")
        table.add_column("Market", justify="center", style="dim")

        flights_sorted = sorted(flights, key=lambda f: (f["depart_date"], f["return_date"], f["price_value"]))

        for f in flights_sorted:
            price_style = _price_color(f["price_value"], threshold, None)
            s = f["stops"]
            stops_text = "Direct" if s == 0 else (f"{s} stop{'s' if isinstance(s, int) and s > 1 else ''}" if s != "Unknown" else "? stops")
            best_marker = " ★" if f.get("is_best") else ""

            table.add_row(
                f["depart_date"],
                f["return_date"],
                f["airline"] + best_marker,
                f["departure"],
                f["arrival"],
                f["duration"],
                stops_text,
                Text(f["price"], style=price_style),
                f.get("current_price_level", ""),
            )

        console.print(table)
        console.print()


def print_alerts(results: list[dict], config: dict) -> None:
    threshold = config.get("price_alert_threshold", 0)
    if not threshold:
        return

    alerts = [r for r in results if r["price_value"] <= threshold]
    if not alerts:
        return

    console.rule("[bold green] PRICE ALERT [/bold green]")
    for a in sorted(alerts, key=lambda r: r["price_value"]):
        console.print(
            f"[bold green]★ {a['price']}[/bold green]  "
            f"{a['origin']} → {a['destination']}  "
            f"[cyan]{a['depart_date']}[/cyan] / back [cyan]{a['return_date']}[/cyan]  "
            f"{a['airline']}  {a['duration']}  "
            f"{'Direct' if a['stops'] == 0 else str(a['stops']) + ' stop(s)'}"
        )
    console.rule()
