"""Command-line entry point for the development server."""

from . import create_app


def main() -> None:
    """Run the Flask application on the configured application port."""
    create_app().run(host="0.0.0.0", port=6555)
