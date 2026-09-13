"""Flag the addresses in an 'Email' column as valid or invalid.

Usage:
    python "email validator.py"                  # file dialogs
    python "email validator.py" in.csv out.xlsx  # no GUI needed
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import pandas as pd

# Rejects the cases the previous pattern (r'^[\w\.-]+@[\w\.-]+\.\w+$') allowed:
# leading/trailing dots, consecutive dots, and a numeric-only TLD.
_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
EMAIL_RE = re.compile(rf"^{_LOCAL}@(?:{_LABEL}\.)+[A-Za-z]{{2,}}$")

MAX_EMAIL_LENGTH = 254  # RFC 5321


def is_valid_email(value) -> bool:
    """True when ``value`` looks like a single well-formed address.

    Non-string input (notably the NaN that pandas puts in empty cells) returns
    False instead of raising. The previous version passed NaN straight to
    re.match, which raised TypeError and aborted the whole run.
    """
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        return False
    return EMAIL_RE.match(candidate) is not None


def load(path: str) -> pd.DataFrame:
    lowered = path.lower()
    if lowered.endswith(".csv"):
        return pd.read_csv(path)
    if lowered.endswith((".xlsx", ".xls")):
        return pd.read_excel(path)
    raise ValueError("Unsupported input format. Use a .csv, .xlsx or .xls file.")


def save(frame: pd.DataFrame, path: str) -> None:
    lowered = path.lower()
    if lowered.endswith(".csv"):
        frame.to_csv(path, index=False)
    elif lowered.endswith((".xlsx", ".xls")):
        frame.to_excel(path, index=False)
    else:
        raise ValueError("Unsupported output format. Use a .csv, .xlsx or .xls file.")


def validate(input_path: str, output_path: str) -> int:
    frame = load(input_path)
    if "Email" not in frame.columns:
        print("No 'Email' column found in the selected file.", file=sys.stderr)
        return 2

    # An 'Email' cell may hold several comma-separated addresses (that is what
    # email-scrapper.py writes), so every address is checked.
    def row_valid(value) -> bool:
        if not isinstance(value, str):
            return False
        parts = [part for part in (p.strip() for p in value.split(",")) if part]
        return bool(parts) and all(is_valid_email(part) for part in parts)

    frame["Valid"] = frame["Email"].map(row_valid)
    save(frame, output_path)

    valid_count = int(frame["Valid"].sum())
    print(f"{valid_count} of {len(frame)} row(s) valid. Saved to {output_path}")
    return 0


def prompt_paths() -> "tuple[str | None, str | None]":
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print("tkinter is unavailable. Pass input and output paths as arguments.",
              file=sys.stderr)
        return None, None

    root = tk.Tk()
    root.withdraw()
    try:
        source = filedialog.askopenfilename(
            title="Select a file",
            filetypes=[("CSV files", "*.csv"), ("Excel files", "*.xlsx *.xls")],
        )
        if not source:
            return None, None
        target = filedialog.asksaveasfilename(
            title="Save validated emails as",
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx"), ("CSV files", "*.csv")],
        )
        return source, (target or None)
    finally:
        root.destroy()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mark each row's Email value as valid or invalid."
    )
    parser.add_argument("input", nargs="?", help="Input .csv/.xlsx (prompts if omitted).")
    parser.add_argument("output", nargs="?", help="Output .csv/.xlsx.")
    args = parser.parse_args(argv)

    input_path, output_path = args.input, args.output
    if not input_path:
        input_path, output_path = prompt_paths()
    if not input_path:
        print("No input file selected.", file=sys.stderr)
        return 2
    if not os.path.exists(input_path):
        print(f"{input_path} does not exist.", file=sys.stderr)
        return 2
    if not output_path:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_validated{ext or '.xlsx'}"

    try:
        return validate(input_path, output_path)
    except Exception as exc:
        print(f"An error occurred: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
