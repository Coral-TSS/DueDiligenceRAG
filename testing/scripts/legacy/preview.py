from pathlib import Path
import importlib.util
import threading
import time
import uvicorn
from dd_agent.app import create_app
from dd_agent.config import Settings

root = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("test_support", root / "tests" / "conftest.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
instance = support.postgres.__wrapped__()
dsn = next(instance) + " dbname=postgres"
app = create_app(Settings(database_url=dsn, data_dir=root / ".local" / "preview-data", port=8766))
app.state.database.migrate()
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8766))
def watch():
    while not (root / ".local" / "preview.stop").exists():
        time.sleep(0.5)
    server.should_exit = True
threading.Thread(target=watch, daemon=True).start()
try:
    server.run()
finally:
    instance.close()
