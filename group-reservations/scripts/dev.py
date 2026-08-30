"""Run the agent API and the static frontend together for local development."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _stream(name: str, process: subprocess.Popen[str]) -> None:
    """Echo one child's output with a short prefix so both logs interleave."""
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(f"[{name}] {line}")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8000, help="uvicorn port for the agent API")
    parser.add_argument("--frontend-port", type=int, default=4173, help="static server port for frontend/")
    parser.add_argument("--host", default="127.0.0.1", help="bind address for both servers")
    parser.add_argument("--no-reload", action="store_true", help="disable uvicorn autoreload")
    args = parser.parse_args(argv)

    src = ROOT / "src"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(src), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}

    if not (ROOT / ".env").exists():
        print("[dev] no .env found; copy .env.example to .env before running the agent", file=sys.stderr)

    api_cmd = [
        sys.executable, "-m", "uvicorn", "groupreservations.api:app",
        "--host", args.host, "--port", str(args.api_port),
    ]
    if not args.no_reload:
        api_cmd += ["--reload", "--reload-dir", str(src)]
    frontend_cmd = [
        sys.executable, "-m", "http.server", str(args.frontend_port),
        "--bind", args.host, "--directory", str(ROOT / "frontend"),
    ]

    # Turn SIGTERM into the same KeyboardInterrupt path as Ctrl+C so the
    # cleanup in `finally` always runs and children are never orphaned.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    procs: list[tuple[str, subprocess.Popen[str]]] = []
    try:
        for name, cmd in (("api", api_cmd), ("frontend", frontend_cmd)):
            proc = subprocess.Popen(
                cmd, cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            procs.append((name, proc))
            threading.Thread(target=_stream, args=(name, proc), daemon=True).start()

        print(f"[dev] API      http://{args.host}:{args.api_port}  (docs at /docs)", flush=True)
        print(f"[dev] frontend http://{args.host}:{args.frontend_port}", flush=True)
        print("[dev] press Ctrl+C to stop both", flush=True)

        while True:
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    print(f"[dev] {name} exited with code {code}; shutting down", file=sys.stderr)
                    return code or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[dev] stopping", file=sys.stderr)
        return 0
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for _, proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
