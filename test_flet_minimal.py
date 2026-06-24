"""Minimal Flet test to verify the basics work."""
import flet as ft


def main(page: ft.Page):
    print("main() called")
    page.title = "Test"
    page.bgcolor = "#121218"
    print("About to add a Text control")
    page.add(
        ft.Text("Hello from Flet 0.85!", size=24, color="#FFFFFF"),
        ft.ElevatedButton("Click me", on_click=lambda _: print("clicked")),
    )
    print("Added controls, calling update")
    page.update()
    print("Done")


ft.app(target=main)
