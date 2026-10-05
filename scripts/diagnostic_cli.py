"""PyInstaller entry point for the Windows diagnostic executable."""

from voiceink_win.diagnostic import main

if __name__ == "__main__":
    raise SystemExit(main())
