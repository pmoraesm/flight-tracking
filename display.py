"""Rich terminal display for flight results."""

from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

console = Console()


def _price_style(is_good_deal: bool) -> str:
    return "bold green" if is_good_deal else "white"


def print_results(results: list) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.rule(f"[bold cyan]Flight Tracker — {now}[/bold cyan]")

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    by_trip: dict = {}
    for r in results:
        by_trip.setdefault(r.get("trip_description", "Untitled trip"), []).append(r)

    for trip_description, trip_results in by_trip.items():
        console.print(f"[bold]{trip_description}[/bold]")

        by_origin: dict = {}
        for r in trip_results:
            by_origin.setdefault(r["origin"], []).append(r)

        for origin, flights in by_origin.items():
            destination = flights[0]["destination"]
            table = Table(
                title=f"[bold]{origin} → {destination}[/bold]",
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
                price_style = _price_style(f.get("is_good_deal", False))
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


def print_alerts(good_deals: list) -> None:
    if not good_deals:
        return

    console.rule("[bold green] PRICE ALERT [/bold green]")
    for a in sorted(good_deals, key=lambda r: r["price_value"]):
        console.print(
            f"[bold green]★ {a.get('trip_description', '')} — {a['price']}[/bold green]  "
            f"{a['origin']} → {a['destination']}  "
            f"[cyan]{a['depart_date']}[/cyan] / back [cyan]{a['return_date']}[/cyan]  "
            f"{a['airline']}  {a['duration']}  "
            f"{'Direct' if a['stops'] == 0 else str(a['stops']) + ' stop(s)'}"
        )
    console.rule()
