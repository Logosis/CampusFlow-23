import argparse
import uvicorn
from _bootstrap import ROOT
from backend.shared.settings import MODULES, PORTS

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("module", choices=MODULES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(f"backend.{args.module}.main:app", host=args.host,
                port=args.port or PORTS[args.module], reload=args.reload)

if __name__ == "__main__":
    main()
