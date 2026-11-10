"""Report whether stdin and stdout are a real terminal (T39 reuses is_interactive)."""

import sys


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def main() -> int:
    stdin_tty = sys.stdin.isatty()
    stdout_tty = sys.stdout.isatty()
    if stdin_tty and stdout_tty:
        print("terminal")
    else:
        print(f"not a terminal (stdin={stdin_tty} stdout={stdout_tty})")
    print(f"platform={sys.platform}")
    return 0 if is_interactive() else 3


if __name__ == "__main__":
    sys.exit(main())
