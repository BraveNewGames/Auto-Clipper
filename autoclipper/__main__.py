"""Start the Auto-Clipper UI: python -m autoclipper [--port N] [--no-browser] [folder]"""
import argparse
import threading
import webbrowser

from .server import DEFAULT_PORT, make_server


def main(argv=None):
    ap = argparse.ArgumentParser(prog="autoclipper", description="Auto-Clipper local UI")
    ap.add_argument("folder", nargs="?", help="session folder to open on start")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (next free one is used if busy)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = ap.parse_args(argv)

    httpd = make_server(args.port)
    if args.folder:
        try:
            res = httpd.app.open({"folder": args.folder})
            if res.get("needs_create"):
                httpd.app.create({"folder": args.folder})
        except Exception as exc:  # keep starting; user can open from the UI
            print(f"Could not open {args.folder}: {exc}")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Auto-Clipper running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
