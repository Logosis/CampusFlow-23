import os
import subprocess
import sys
import _bootstrap

module = os.environ["APP_MODULE"]
subprocess.run([sys.executable, "scripts/manage_db.py", "migrate", "--module", module], check=True)
os.execv(sys.executable, [sys.executable, "scripts/serve.py", module,
                         "--host", "0.0.0.0", "--port", "8000"])
