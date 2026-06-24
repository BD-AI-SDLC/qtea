"""Test if direct view population works in Flet 0.85."""
import flet as ft


def main(page: ft.Page):
    page.title = "Routing Test"
    page.bgcolor = "#121218"
    print("main() called, route =", page.route)

    page.views.clear()
    page.views.append(
        ft.View(
            route="/",
            controls=[
                ft.Text("HELLO FROM VIEW", size=32, color="#FFFFFF"),
                ft.ElevatedButton("Click me"),
            ],
            bgcolor="#121218",
        )
    )
    page.update()
    print("View populated")


ft.app(target=main)
