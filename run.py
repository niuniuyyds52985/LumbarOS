#!/usr/bin/env python3
"""Launch Lumbar Lab; simulation has no third-party requirements."""
import argparse
import importlib
import sys

if sys.version_info < (3, 10):
    print("Lumbar Lab requires Python 3.10+. On Windows, double-click start_windows.bat.")
    raise SystemExit(1)

import threading
import webbrowser
from lumbar.server import Server
from lumbar.service import AppService


def load_actuator_factory(spec):
    """Import only the module/function explicitly selected on the command line."""
    if not isinstance(spec, str) or spec.count(":") != 1:
        raise ValueError("actuator factory must use module:callable, e.g. integrations.hardware_adapter:create_actuator")
    module_name, name = spec.split(":")
    if not module_name or not all(part.isidentifier() for part in module_name.split(".")) or not name.isidentifier():
        raise ValueError("actuator factory must name a Python module and a function as module:callable")
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, name)
    except Exception as exc:
        raise ValueError(f"cannot load actuator factory {spec}: {exc}") from exc
    if not callable(factory):
        raise ValueError(f"actuator factory {spec} is not callable")
    return factory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lumbar Lab local demo")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--actuator-factory", metavar="MODULE:CALLABLE",
                        help="explicit adapter factory, e.g. integrations.hardware_adapter:create_actuator; default serial output is disabled")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    app_options = {}
    if args.actuator_factory is not None:
        try:
            app_options["actuator_factory"] = load_actuator_factory(args.actuator_factory)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        app = AppService(**app_options)
    except Exception as exc:
        print(f"Cannot initialize Lumbar Lab or the selected actuator factory: {exc}", file=sys.stderr)
        return 1
    try:
        server = Server(("127.0.0.1", args.port), app)
    except OSError as exc:
        app.close()
        print(f"Cannot open port {args.port}: {exc}\nTry: python run.py --port 8766")
        return 1
    stopped = threading.Event()
    def control_loop():
        while not stopped.wait(0.05):
            try:
                app.tick()
            except Exception as exc:
                app.fail("内部错误 · 已停止，请重启实验台")
                print(f"Control loop stopped: {exc}", file=sys.stderr)
                break
    worker = threading.Thread(target=control_loop, name="lumbar-control", daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Lumbar Lab: {url}\nSimulation mode. Press Ctrl+C to close.", flush=True)
    if args.actuator_factory is not None:
        print(f"Actuator factory loaded: {args.actuator_factory}. Startup remains in simulation mode.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        worker.join(timeout=2)
        app.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
